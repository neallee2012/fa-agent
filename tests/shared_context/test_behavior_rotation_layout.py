"""Native-scale history bytes through actual Table SDK serialization, offline."""
import json
import copy
import hashlib
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from azure.data.tables import TableClient

from shared_context.behavior_migration import (
    DEFAULT_INVENTORY_MAX_BYTES, DEFAULT_INVENTORY_MAX_ENTITIES, MigrationStore, installed_origin,
)
from shared_context.behavior_runtime_rotation import (
    RuntimeRotation, RotationHistory, _measure_layout,
    _read_shared_preimages, _shared_preimage_writes, _snapshot_store, rotation_key,
)
from shared_context.behavior_runtime_transition import _payload_hash
from shared_context.behavior_store import _Row, ControlChanges, ControlRequest, ControlWrite, InlineMaterial
from shared_context.records import Record
from shared_context.source_materials import digest
from shared_context.errors import Problem
from test_behavior_rotation_cutoff import publish_worker, publish_inbox
from test_behavior_rotation_settlement_history import current_cancelled, next_rotation, settle
from test_behavior_runtime_rotation import rotated
from test_behavior_runtime_rotation_sources import QueryTransport
from test_behavior_store import Credential


def synthetic_proof(size, kind):
    body = {"kind": kind, "evidence_mode": "offline-native-contract", "observations": "x" * size}
    raw = json.dumps(body, separators=(",", ":")).encode()
    body["observations"] = "x" * (size - (len(raw) - size))
    result = json.dumps(body, separators=(",", ":")).encode()
    assert len(result) == size
    return result


@pytest.fixture
def native_scale(current_cancelled):
    c = current_cancelled
    now = c.clock[0]
    worker, proof = publish_worker(c.store, c.config, now, "small-worker")
    proof = proof.model_copy(update={
        "evidence_id": "native-scale-worker", "observed_at": now + timedelta(microseconds=1),
        "proof": synthetic_proof(4096, "worker-observation"),
    })
    worker.clock = lambda: proof.observed_at
    worker.adapter = replace(worker.adapter, verify_readiness=lambda _: proof)
    worker.publish_readiness(proof.evidence_id)
    c.clock[0] = proof.observed_at
    _, channel, inbox = publish_inbox(c.store, c.config, c.clock[0], proof, "small-inbox")
    c.clock[0] += timedelta(microseconds=1)
    inbox = inbox.model_copy(update={
        "evidence_id": "native-scale-inbox", "proof": synthetic_proof(1024, "receiver-observation"),
        "readiness": inbox.readiness.model_copy(update={
            "observed_at": c.clock[0], "proof": synthetic_proof(6144, "delivery-observation"),
        }),
    })
    channel.core.clock = lambda: c.clock[0]
    channel.registration = replace(channel.registration, verify_deployment=lambda _: inbox)
    channel.publish_readiness(inbox.evidence_id)
    settle(c)
    service = next_rotation(c, suffix=getattr(c, "next_suffix", ""))
    transport = QueryTransport()
    transport.backing = c.h.table.backing
    with TableClient(c.config.table.endpoint, c.config.table.scope.table_name,
                     credential=Credential(), transport=transport) as client:
        store = MigrationStore(client, c.config.table.scope)
        service = RuntimeRotation(store, service.old, service.new, service.evidence,
                                  native_grants=service.grants, clock=service.clock, allow_apply=True)
        yield SimpleNamespace(c=c, service=service, store=store, transport=transport)


def test_native_scale_preimages_fit_real_entity_and_single_egt(native_scale, monkeypatch):
    h = native_scale
    plan = h.service.plan(h.c.principal, rotation_id="native-scale-rotation")
    approval = h.service.authorize(h.c.principal, plan, decision_id="native-scale-approved",
                                   expires_at=h.c.clock[0] + timedelta(minutes=5))
    before = h.store.read_inventory()
    encoded = []
    original = h.store._entity
    def entity(key, model):
        raw = json.dumps(model.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        encoded.append((key, len(raw)))
        return original(key, model)
    monkeypatch.setattr(h.store, "_entity", entity)
    posts = sum(r.method == "POST" for r in h.transport.requests)
    try:
        h.service.apply(h.c.principal, plan, approval)
    except Problem as error:
        print("actual _entity largest payload:", max(encoded, key=lambda pair: pair[1])[1], flush=True)
        assert error.body["error"]["code"] == "BEHAVIOR_ENTITY_TOO_LARGE"
        assert sum(r.method == "POST" for r in h.transport.requests) == posts
        assert h.store.read_inventory().entity_sha256 == before.entity_sha256
        raise
    assert sum(r.method == "POST" for r in h.transport.requests) == posts + 1
    assert max(size for _, size in encoded) <= 49152
    wire = [r for r in h.transport.requests if r.method == "POST"][-1]
    assert len(wire.body) <= 4 * 1024 * 1024 and len(h.transport.parts[-1]) <= 100
    assert installed_origin(MigrationStore(h.store._client._client, h.store._scope), h.service.new) == h.c.h.original
    h.service.verify(h.c.principal, plan, approval)
    print(json.dumps({"max_entity_bytes": max(size for _, size in encoded),
                      "operations": len(h.transport.parts[-1]), "multipart_bytes": len(wire.body)}), flush=True)


def approved_layout(h):
    plan = h.service.plan(h.c.principal, rotation_id="bounded-layout")
    approval = h.service.authorize(h.c.principal, plan, decision_id="bounded-approved",
                                   expires_at=h.c.clock[0] + timedelta(minutes=5))
    return plan, approval


def test_ack_loss_reconciles_only_original_whole_egt(native_scale):
    h = native_scale
    plan, approval = approved_layout(h)
    before = h.store.read_inventory()
    posts = sum(r.method == "POST" for r in h.transport.requests)
    h.transport.after_apply = "lost"
    with pytest.raises(Problem) as error:
        h.service.apply(h.c.principal, plan, approval)
    assert error.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    h.transport.after_apply = None
    assert sum(r.method == "POST" for r in h.transport.requests) == posts + 1
    h.service.reconcile(h.c.principal, plan, approval)
    h.service.verify(h.c.principal, plan, approval)
    assert sum(r.method == "POST" for r in h.transport.requests) == posts + 1
    after = h.store.read_inventory()
    changed = {image.row_id for image in plan.images} | {rotation_key("current", plan.authority_domain)}
    assert all(after.rows[key] == row for key, row in before.rows.items() if key not in changed)
    assert all(after.receipts[key] == row for key, row in before.receipts.items())


@pytest.mark.parametrize("mutation", ["missing", "hash", "material", "duplicate", "extra", "scope", "cutoff", "current"])
def test_chunk_and_current_tampering_never_becomes_a_valid_cutover(native_scale, mutation):
    h = native_scale
    plan, approval = approved_layout(h)
    h.service.apply(h.c.principal, plan, approval)
    inventory = h.store.read_inventory()
    rows = copy.deepcopy(dict(inventory.rows))
    hashes = dict(inventory.entity_sha256)
    history_key = rotation_key("history", plan.rotation_id)
    history = RotationHistory.model_validate_json(json.dumps(rows[history_key].payload["data"]))
    chunk_key = history.shared_preimage_chunks[0].row_id
    if mutation == "missing":
        rows.pop(chunk_key)
    elif mutation in {"hash", "material", "scope", "cutoff"}:
        row = rows[chunk_key]
        payload = copy.deepcopy(row.payload)
        if mutation == "hash":
            payload["data"]["material_sha256"] = "f" * 64
        elif mutation == "material":
            payload["material"] = InlineMaterial(data=b"changed", sha256=hashlib.sha256(b"changed").hexdigest()).model_dump(mode="json")
        elif mutation == "scope":
            payload["data"]["configuration_sha256"] = "f" * 64
        else:
            payload["data"]["inventory_sha256"] = "f" * 64
        rows[chunk_key] = Record(payload, row.etag)
        hashes["behavior:v1:row:" + chunk_key] = _payload_hash(payload)
    elif mutation == "duplicate":
        row = rows[history_key]
        payload = copy.deepcopy(row.payload)
        payload["data"]["shared_preimage_chunks"].append(payload["data"]["shared_preimage_chunks"][0])
        rows[history_key] = Record(payload, row.etag)
        hashes["behavior:v1:row:" + history_key] = _payload_hash(payload)
    elif mutation == "extra":
        row = rows[chunk_key]
        key = "unreferenced-preimage-chunk"
        payload = {**row.payload, "row_id": key}
        rows[key] = Record(payload, row.etag)
        hashes["behavior:v1:row:" + key] = _payload_hash(payload)
    else:
        key = next(image.row_id for image in plan.images if image.after["data"].get("kind") == "worker_pool")
        row = rows[key]
        payload = copy.deepcopy(row.payload)
        payload["data"]["pending"] = ["unknown-job"]
        rows[key] = Record(payload, row.etag)
        hashes["behavior:v1:row:" + key] = _payload_hash(payload)
    before = h.store.read_inventory().entity_sha256
    with pytest.raises((Problem, ValueError)):
        installed_origin(_snapshot_store(h.store, replace(inventory, rows=rows, entity_sha256=hashes), h.service.new),
                         h.service.new)
    assert h.store.read_inventory().entity_sha256 == before


@pytest.mark.parametrize("fault,code", [
    ("entity", "BEHAVIOR_ENTITY_TOO_LARGE"), ("operations", "BEHAVIOR_BATCH_TOO_LARGE"),
    ("multipart", "BEHAVIOR_BATCH_TOO_LARGE"),
])
def test_plan_checks_actual_wire_layout_before_any_approval_or_write(native_scale, monkeypatch, fault, code):
    h = native_scale
    original = h.service._layout
    def layout(*args):
        changes = original(*args)
        if fault == "entity":
            extra = (ControlWrite(row_id="oversized-layout", mode="create", data={"payload": "x" * 49152}),)
        elif fault == "operations":
            extra = tuple(ControlWrite(row_id=f"extra-{i}", mode="create", data={"value": i})
                          for i in range(99 - len(changes.writes)))
        else:
            extra = tuple(ControlWrite(row_id=f"wire-{i}", mode="create", data={"payload": "x" * 47000})
                          for i in range(70))
        # Deliberately exceed the prospective write contract to exercise the
        # plan's operation ceiling before any partial row can be sent.
        return changes.model_copy(update={"writes": (*changes.writes, *extra)})
    monkeypatch.setattr(h.service, "_layout", layout)
    before = h.store.read_inventory().entity_sha256
    posts = sum(r.method == "POST" for r in h.transport.requests)
    with pytest.raises(Problem) as error:
        h.service.plan(h.c.principal, rotation_id="must-fail-before-approval")
    assert error.value.body["error"]["code"] == code
    assert sum(r.method == "POST" for r in h.transport.requests) == posts
    assert h.store.read_inventory().entity_sha256 == before


@pytest.mark.parametrize("limit,measurement", [
    ("max_payload_bytes", "max_payload_bytes"), ("max_operations", "operations"),
    ("max_batch_bytes", "multipart_bytes"),
])
def test_exact_wire_limit_passes_and_one_byte_or_operation_less_denies(native_scale, limit, measurement):
    h = native_scale
    plan, approval = approved_layout(h)
    inventory = h.store.read_inventory()
    changes = h.service._layout(plan, approval, inventory, h.c.clock[0])
    request = h.service._request(plan, approval)
    original_scope = h.store._scope
    measured = _measure_layout(h.store, inventory, h.service.old, h.service.new, request, changes)
    before_posts = sum(r.method == "POST" for r in h.transport.requests)
    try:
        h.store._scope = original_scope.model_copy(update={limit: measured[measurement]})
        assert _measure_layout(h.store, inventory, h.service.old, h.service.new, request, changes) == measured
        h.store._scope = original_scope.model_copy(update={limit: measured[measurement] - 1})
        with pytest.raises(Problem) as error:
            _measure_layout(h.store, inventory, h.service.old, h.service.new, request, changes)
        assert error.value.status == 413
    finally:
        h.store._scope = original_scope
    assert sum(r.method == "POST" for r in h.transport.requests) == before_posts


def test_full_100_operation_layout_serializes_without_sending(native_scale):
    h = native_scale
    plan, approval = approved_layout(h)
    inventory = h.store.read_inventory()
    changes = h.service._layout(plan, approval, inventory, h.c.clock[0])
    extra = tuple(ControlWrite(row_id=f"layout-capacity-{i}", mode="create", data={"value": i})
                  for i in range(98 - len(changes.writes)))
    changes = ControlChanges(writes=(*changes.writes, *extra), result=changes.result)
    posts = sum(r.method == "POST" for r in h.transport.requests)
    result = _measure_layout(h.store, inventory, h.service.old, h.service.new,
                             h.service._request(plan, approval), changes)
    assert result["operations"] == 100 and result["multipart_bytes"] <= 4 * 1024 * 1024
    assert sum(r.method == "POST" for r in h.transport.requests) == posts


def test_receipt_covered_but_unreferenced_preimage_chunk_is_still_unknown(native_scale):
    h = native_scale
    plan, approval = approved_layout(h)
    h.service.apply(h.c.principal, plan, approval)
    inventory = h.store.read_inventory()
    history = RotationHistory.model_validate_json(json.dumps(
        inventory.rows[rotation_key("history", plan.rotation_id)].payload["data"]))
    row = inventory.rows[history.shared_preimage_chunks[0].row_id]
    material = InlineMaterial.model_validate_json(json.dumps(row.payload["material"]))
    h.store.transact(ControlRequest(request_id="append-extra-typed-chunk", request_digest="a" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(row_id="unreferenced-typed-chunk", mode="create",
                                                       data=row.payload["data"], material=material),), result={}))
    before = h.store.read_inventory().entity_sha256
    with pytest.raises(Problem, match="unclassified late current-runtime row"):
        installed_origin(MigrationStore(h.store._client._client, h.store._scope), h.service.new)
    assert h.store.read_inventory().entity_sha256 == before


def test_exact_48k_storage_payload_is_preserved_across_multiple_bounded_chunks(native_scale, monkeypatch):
    """Storage framing test, not permission to introduce an untyped domain row."""
    from shared_context import behavior_runtime_rotation as rotation
    h = native_scale
    plan, approval = approved_layout(h)
    row_id = "synthetic-full-size-preimage"
    base = _Row(binding=h.store._binding, row_id=row_id, data={"value": ""})
    padding = 49152 - len(json.dumps(base.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode())
    data = {"value": "x" * padding}
    request = ControlRequest(request_id="create-full-size-storage-row", request_digest=digest(data))
    h.store.transact(request, lambda _: ControlChanges(
        writes=(ControlWrite(row_id=row_id, mode="create", data=data),), result={"storage_only": True}))
    inventory = h.store.read_inventory()
    original = inventory.rows[row_id]
    assert len(h.store._entity("behavior:v1:row:" + row_id, _Row.model_validate_json(
        json.dumps(original.payload)))["Payload"]) == 49152
    monkeypatch.setattr(rotation, "shared_keys", lambda _: {row_id})
    writes, refs = _shared_preimage_writes(inventory, h.service.old, plan)
    assert len(writes) >= 3
    rows = dict(inventory.rows)
    max_entity = 0
    for write in writes:
        value = _Row(binding=h.store._binding, row_id=write.row_id, data=write.data, material=write.material)
        max_entity = max(max_entity, len(h.store._entity("behavior:v1:row:" + write.row_id, value)["Payload"]))
        assert len(write.material.data) <= 24576 < 32768
        rows[write.row_id] = Record(value.model_dump(mode="json"), 'W/"synthetic-chunk"')
    history = SimpleNamespace(schema_version="fa-runtime-rotation-history-v3", plan=plan,
                              shared_preimages=(), shared_preimage_chunks=refs)
    restored, = _read_shared_preimages(replace(inventory, rows=rows), history)
    assert restored.before == restored.after == original.payload and restored.before_etag == original.etag
    assert max_entity <= 49152
    print(json.dumps({"single_preimage_payload": 49152, "chunks": len(writes),
                      "max_chunk_row_payload": max_entity}), flush=True)


@pytest.mark.parametrize("bound", ["entities", "bytes"])
def test_complete_post_cutover_inventory_is_bounded_before_send(native_scale, bound):
    h = native_scale
    plan, approval = approved_layout(h)
    inventory = h.store.read_inventory()
    changes = h.service._layout(plan, approval, inventory, h.c.clock[0])
    rows = dict(inventory.rows)
    count, padding = ((DEFAULT_INVENTORY_MAX_ENTITIES, 0) if bound == "entities"
                      else (DEFAULT_INVENTORY_MAX_BYTES // 47000 + 1, 47000))
    for index in range(count):
        key = f"synthetic-inventory-capacity-{index}"
        value = _Row(binding=h.store._binding, row_id=key, data={"value": "x" * padding})
        assert len(h.store._entity("behavior:v1:row:" + key, value)["Payload"]) <= 49152
        rows[key] = Record(value.model_dump(mode="json"), 'W/"capacity"')
    before = sum(r.method == "POST" for r in h.transport.requests)
    with pytest.raises(Problem) as error:
        _measure_layout(h.store, replace(inventory, rows=rows), h.service.old, h.service.new,
                        h.service._request(plan, approval), changes)
    assert error.value.body["error"]["code"] == "ROTATION_INVENTORY_LIMIT"
    assert sum(r.method == "POST" for r in h.transport.requests) == before


def test_third_real_rotation_preserves_full_lineage_with_native_scale_proofs(request, monkeypatch):
    c = request.getfixturevalue("current_cancelled")
    settle(c)
    second = next_rotation(c)
    h = SimpleNamespace(service=second, new=second.new, old=second.old, table=c.h.table, store=second.store,
                        original=c.h.original, principal=c.principal, paths=c.h.paths,
                        h=SimpleNamespace(time=c.clock[0]))
    successor = current_cancelled.__wrapped__(h, SimpleNamespace(id_suffix="-second"))
    successor.next_suffix = "-third"
    fixture = native_scale.__wrapped__(successor)
    try:
        h3 = next(fixture)
        test_native_scale_preimages_fit_real_entity_and_single_egt(h3, monkeypatch)
        histories = [row for row in h3.store.read_inventory().rows.values()
                     if row.payload["data"].get("schema_version") == "fa-runtime-rotation-history-v3"]
        assert len(histories) == 3
    finally:
        fixture.close()
