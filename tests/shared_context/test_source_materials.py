"""Offline contracts and real SDK/local HttpTransport, not Azure/Hosted PASS."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from azure.core.exceptions import ServiceRequestError
from azure.storage.blob import ContainerClient

from conftest import TENANT
from test_backup_store import BlobTransport, Credential
from test_behavior_store import FakeTable, SCOPE, wire_store
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite
from shared_context.errors import Problem
from shared_context.m3_store import BlobArtifactStore
from shared_context.source_materials import (
    AuthenticatedSourceContext, InventoryProtectionVerifier, NativeProtectionState,
    NoAutoGCConfiguration, SourceComposition, SourceInput, SourceMaterials, SourcePlan, SourceSnapshot, SourceSnapshotPlan,
    control_write, digest,
)

NOW = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
CONTEXT = AuthenticatedSourceContext(TENANT, "team-one", "agent-a", "ingress-one", ("work", "profile"))
OTHER = replace(CONTEXT, actor_id="agent-b")
CONFIG = NoAutoGCConfiguration(
    authority_domain=SCOPE.authority_domain, generation="g1",
    container_url="https://synthetic.blob.core.windows.net/originals",
    protected_prefix=f"m3/v1/{TENANT}/", configuration_version="configuration-v1",
    evidence_sha256="a" * 64,
)


def native_state(**changes):
    state = NativeProtectionState(
        authority_domain=CONFIG.authority_domain, generation=CONFIG.generation,
        container_url=CONFIG.container_url, protected_prefix=CONFIG.protected_prefix,
        configuration_version=CONFIG.configuration_version, evidence_sha256=CONFIG.evidence_sha256,
        observed_at=NOW - timedelta(minutes=1), valid_until=NOW + timedelta(hours=1),
        blob_service_etag='"service-v1"', management_policy_etag='"policy-v1"',
        versioning_enabled=True, management_policy={"rules": []},
        automatic_delete_consumers=(), inventory_version="inventory-v1",
    )
    return replace(state, **changes)


def code(error):
    return error.value.body["error"]["code"]


@pytest.fixture
def source_service():
    table = FakeTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    service = SourceMaterials(store)
    return service, table


def input_source(source_id="event-one", version=1, **changes):
    return SourceInput(source_id=source_id, version=version, purpose="work",
                       schema_id="ingress-v1", media_type="application/octet-stream", **changes)


def admitted(service, data=b"cobalt", **changes):
    plan = service.plan(CONTEXT, input_source(**changes), data)
    receipt = service.commit(CONTEXT, plan)
    return plan, receipt


@pytest.mark.parametrize("original", [
    "中文，完整保留\r\n第二行\n尾端\n".encode("utf-8"),
    b'{"a": 1, "a":2,\n "b": "cobalt"}\r\n',
    bytes(range(256)) + b"\x00\xff\r\n", b"", b"x" * (32 * 1024),
], ids=["chinese-newlines", "duplicate-json", "binary", "empty", "inline-bound"])
def test_inline_exact_original_bytes_and_reconstructed_retry(source_service, original):
    service, table = source_service
    plan, receipt = admitted(service, original)
    reconstructed = SourcePlan.model_validate_json(plan.model_dump_json())
    before = len(table.submissions)
    assert service.commit(CONTEXT, reconstructed) == receipt
    assert service.reconcile(CONTEXT, reconstructed) == receipt
    assert len(table.submissions) == before
    exact = service.read_exact(CONTEXT, plan.reference)
    assert exact.original == original
    assert exact.event.reference.byte_length == len(original)
    assert exact.event.reference.sha256 == hashlib.sha256(original).hexdigest()
    assert exact.event.source_actor == CONTEXT.actor_id
    assert exact.receipt == receipt
    operations = table.submissions[-1]
    assert len(operations) == 5  # guard, receipt, event/original, head, scope sequence
    assert all(op[1]["PartitionKey"] == SCOPE.partition_key for op in operations)


def test_duplicate_json_keys_are_not_silently_canonicalized(source_service):
    service, _ = source_service
    data = b'{ "x": 1, "x": 2 }\r\n'
    plan, _ = admitted(service, data)
    parsed = json.dumps(json.loads(data)).encode()
    assert service.read_exact(CONTEXT, plan.reference).original != parsed
    with pytest.raises(Problem) as mismatch:
        service.commit(CONTEXT, service.plan(CONTEXT, plan.source, parsed))
    assert code(mismatch) == "BEHAVIOR_REQUEST_CONFLICT"


@pytest.mark.parametrize("failure", ["before", "after"])
def test_unknown_retains_original_request_and_reconstruction_only_reconciles(source_service, failure):
    service, table = source_service
    plan = service.plan(CONTEXT, input_source(), b"original")
    table.failure = failure
    with pytest.raises(Problem) as unknown:
        service.commit(CONTEXT, plan)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert unknown.value.body["error"]["request_id"] == plan.request.request_id
    count = len(table.submissions)
    restarted = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    retained = SourcePlan.model_validate_json(plan.model_dump_json())
    assert retained.original == b"original"
    if failure == "after":
        assert restarted.reconcile(CONTEXT, retained).payload["result"]["reference"]["sha256"] == plan.reference.sha256
    else:
        with pytest.raises(Problem) as absent:
            restarted.reconcile(CONTEXT, retained)
        assert code(absent) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert len(table.submissions) == count


def test_private_wrong_actor_team_and_same_id_different_source_actor(source_service):
    service, _ = source_service
    private, _ = admitted(service, visibility="private")
    for context in (OTHER, replace(CONTEXT, team_id="team-other"), replace(CONTEXT, tenant_id="other")):
        with pytest.raises(Problem) as denied:
            service.read_exact(context, private.reference)
        assert code(denied) == "SOURCE_SCOPE_DENIED"
    shared, _ = admitted(service, source_id="shared")
    with pytest.raises(Problem) as denied:
        service.commit(OTHER, shared)
    assert code(denied) == "SOURCE_REQUEST_MISMATCH"
    assert service.read_exact(OTHER, shared.reference).original == b"cobalt"
    replacement = service.plan(OTHER, input_source(source_id="shared", version=2), b"other actor's change")
    with pytest.raises(Problem) as overwrite:
        service.commit(OTHER, replacement)
    assert code(overwrite) == "SOURCE_SCOPE_DENIED"
    with pytest.raises(Problem):
        service.plan(CONTEXT, input_source(source_id="leak", necessary=(private.reference,)), b"secret")


def test_new_versions_preserve_original_and_stale_exact_current_fails(source_service):
    service, _ = source_service
    first, _ = admitted(service, b"original-v1")
    second, _ = admitted(service, b"original-v2", version=2)
    assert service.read_exact(CONTEXT, first.reference).original == b"original-v1"
    assert service.read_exact(CONTEXT, second.reference, require_current=True).original == b"original-v2"
    with pytest.raises(Problem) as stale:
        service.read_exact(CONTEXT, first.reference, require_current=True)
    assert code(stale) == "SOURCE_STALE"
    with pytest.raises(Problem):
        admitted(service, b"skipped", version=4)


def test_shared_guard_race_has_no_partial_event_head_or_sequence(source_service):
    service, table = source_service
    competitor = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    original = service.plan(CONTEXT, input_source(), b"mine")
    winner = competitor.plan(CONTEXT, input_source(source_id="other-event"), b"other")
    table.before_submit = lambda: competitor.commit(CONTEXT, winner)
    with pytest.raises(Problem) as conflict:
        service.commit(CONTEXT, original)
    assert code(conflict) == "BEHAVIOR_CONFLICT"
    rows = service.read_snapshot((original.reference.key, original.reference.head_key))
    assert all(value is None for value in rows.values())
    assert competitor.read_exact(CONTEXT, winner.reference).event.sequence == 1


def test_exact_source_read_uses_public_guarded_snapshot_without_writes(source_service):
    service, table = source_service
    plan, _ = admitted(service)
    assert service.read_snapshot == service.store.read_snapshot
    before = len(table.submissions)
    assert service.read_exact(CONTEXT, plan.reference).original == plan.original
    assert len(table.submissions) == before
    competitor = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    other = competitor.plan(CONTEXT, input_source(source_id="concurrent"), b"other")
    # An unrelated source commit moves the guard; the rows this read used are unchanged.
    table.after_get = lambda _: competitor.commit(CONTEXT, other)
    assert service.read_exact(CONTEXT, plan.reference).original == plan.original
    assert len(table.submissions) == before


def test_composed_admission_binds_shared_predicates_source_bytes_and_required_writes(source_service):
    service, table = source_service
    quota, work = control_key("quota", "team-one"), control_key("work", "work-one")
    service.store.transact(ControlRequest(request_id="quota-seed", request_digest="a" * 64),
                           lambda _: ControlChanges(writes=(
                               ControlWrite(row_id=quota, mode="create", data={"remaining": 1}),
                           ), result={}))
    composition = SourceComposition(transition_digest=digest({"work_id": "work-one", "cost": 1}),
                                    additional_reads=(quota,), required_write_keys=(quota, work))
    plan = service.plan(CONTEXT, input_source(), b"necessary work input", composition=composition)
    service.validate_attachment(CONTEXT, plan)
    before = len(table.submissions)
    with pytest.raises(Problem) as incomplete:
        service.store.transact(plan.request, lambda rows: service.compose_attachment(
            CONTEXT, plan, rows, ControlChanges(writes=(), result={})))
    assert code(incomplete) == "SOURCE_COMPOSITION_INCOMPLETE"
    assert len(table.submissions) == before

    def decide(rows):
        remaining = rows[quota].payload["data"]["remaining"]
        assert remaining == 1
        return service.compose_attachment(CONTEXT, plan, rows, ControlChanges(writes=(
            ControlWrite(row_id=quota, mode="replace", data={"remaining": remaining - 1}),
            ControlWrite(row_id=work, mode="create", data={"state": "accepted", "source": plan.reference.key}),
        ), result={"work_id": "work-one"}))

    receipt = service.store.transact(plan.request, decide)
    assert len(table.submissions[-1]) == 7
    assert service.read_exact(CONTEXT, plan.reference).receipt == receipt
    assert receipt.payload["result"]["transition"] == {"work_id": "work-one"}
    assert service.read_snapshot((quota,))[quota].payload["data"]["remaining"] == 0


def test_real_table_sdk_serializes_inline_binary_with_immutable_receipt():
    store, transport = wire_store()
    store.initialize()
    service = SourceMaterials(store)
    plan, receipt = admitted(service, "鈷藍色\r\n".encode() + bytes(range(32)))
    assert service.read_exact(CONTEXT, plan.reference).original == plan.original
    assert len(transport.parts[-1]) == 5
    assert transport.parts[-1][0][2]["If-Match"]
    assert receipt.payload["request"]["request_id"] == plan.request.request_id


@pytest.fixture
def large_service(source_service):
    simple, table = source_service
    transport = BlobTransport([])
    with ContainerClient("https://synthetic.blob.core.windows.net", "originals",
                         credential=Credential(), transport=transport,
                         max_single_put_size=512 * 1024, max_single_get_size=512 * 1024) as container:
        verifier = InventoryProtectionVerifier(lambda: native_state(), clock=lambda: NOW)
        service = SourceMaterials(simple.store,
                                  blob_store=BlobArtifactStore(container, TENANT),
                                  protection=CONFIG, protection_verifier=verifier)
        service.initialize_protection()
        yield service, table, transport


NATIVE = "2026-09-15T14:00:00.0000000Z"
ETAG = '"0xSOURCE"'


def blob_headers(plan):
    artifact = control_key("source-object", plan.reference.scope.key, plan.reference.source_id)
    version = control_key("source-object-version", plan.reference.key)[:32]
    return {
        "ETag": ETAG, "x-ms-version-id": NATIVE, "Content-Length": str(len(plan.original)),
        "Content-Type": "application/octet-stream", "x-ms-blob-type": "BlockBlob",
        "Last-Modified": "Tue, 15 Sep 2026 14:00:00 GMT",
        "x-ms-meta-tenant_id": TENANT, "x-ms-meta-team": CONTEXT.team_id,
        "x-ms-meta-artifact_id": artifact, "x-ms-meta-logical_version_id": version,
        "x-ms-meta-sha256": plan.reference.sha256, "x-ms-meta-bytes": str(len(plan.original)),
    }


def blob_reads(plan, data=None):
    headers = blob_headers(plan)
    return [(200, headers), (206, {**headers, "Content-Range": f"bytes 0-{len(plan.original)-1}/{len(plan.original)}"},
                            plan.original if data is None else data)]


def prepared_large(service, transport):
    plan = service.plan(CONTEXT, input_source(), b"\x00cobalt\r\n" * 5000)
    headers = blob_headers(plan)
    transport.responses.extend([(201, {"ETag": ETAG, "x-ms-version-id": NATIVE}), (200, headers)] + blob_reads(plan))
    service.prepare_large(CONTEXT, plan)
    transport.responses.extend(blob_reads(plan))
    return plan


def test_real_blob_sdk_large_exact_version_digest_and_create_only(large_service):
    service, _, transport = large_service
    plan = prepared_large(service, transport)
    receipt = service.commit(CONTEXT, plan)
    transport.responses.extend(blob_reads(plan))
    exact = service.read_exact(CONTEXT, plan.reference)
    assert exact.original == plan.original and exact.receipt == receipt
    assert exact.event.protection == CONFIG
    assert exact.event.blob.receipt.native_version == NATIVE
    assert exact.event.blob.blob_name.startswith(CONFIG.protected_prefix)
    puts = [item for item in transport.requests if item.method == "PUT"]
    assert len(puts) == 1 and puts[0].body == plan.original
    assert puts[0].headers["If-None-Match"] == "*"
    for item in transport.requests[1:]:
        assert parse_qs(urlsplit(item.url).query)["versionid"] == [NATIVE]
    for item in transport.requests:
        if item.method == "GET":
            assert item.headers["If-Match"] == ETAG


@pytest.mark.parametrize("changes", [
    {"versioning_enabled": False},
    {"automatic_delete_consumers": ("orphan-cleaner-v2",)},
    {"management_policy": {}},
    {"management_policy": {"rules": [{"enabled": True, "definition": {
        "actions": {"version": {"delete": {"daysAfterCreationGreaterThan": 30}}}}}]}},
    {"valid_until": NOW},
    {"generation": "g2"},
    {"blob_service_etag": ""},
])
def test_no_auto_gc_verifier_rejects_unsafe_incomplete_stale_native_inventory(changes):
    verifier = InventoryProtectionVerifier(lambda: native_state(**changes), clock=lambda: NOW)
    with pytest.raises(Problem):
        verifier.verify(CONFIG)


def test_large_input_disabled_without_verified_server_protection(source_service):
    service, table = source_service
    before = len(table.submissions)
    with pytest.raises(Problem) as absent:
        service.plan(CONTEXT, input_source(), b"x" * (32 * 1024 + 1))
    assert code(absent) == "SOURCE_PROTECTION_UNVERIFIED"
    assert len(table.submissions) == before
    with pytest.raises(Problem) as too_large:
        service.plan(CONTEXT, input_source(), b"x" * (512 * 1024 + 1))
    assert code(too_large) == "SOURCE_TOO_LARGE"
    with pytest.raises(Exception):
        input_source().model_validate({**input_source().model_dump(), "protected": True})


def test_protected_preparation_generation_race_rejects_attachment(large_service):
    service, table, transport = large_service
    plan = prepared_large(service, transport)
    other = BehaviorStore(FakeTable(table.backing), SCOPE)
    changed = CONFIG.model_copy(update={"generation": "g2"})
    table.before_submit = lambda: other.transact(
        ControlRequest(request_id="rotate-protection", request_digest="f" * 64, read_set=(CONFIG.key,)),
        lambda _: ControlChanges(writes=(control_write(CONFIG.key, changed, "replace"),), result={}),
    )
    with pytest.raises(Problem) as race:
        service.commit(CONTEXT, plan)
    assert code(race) == "BEHAVIOR_CONFLICT"
    with pytest.raises(Problem) as old:
        service.commit(CONTEXT, plan)
    assert code(old) == "SOURCE_PROTECTION_CHANGED"
    assert service.read_snapshot((plan.reference.key,))[plan.reference.key] is None
    assert len([item for item in transport.requests if item.method == "PUT"]) == 1


def test_unknown_upload_is_durable_and_reconstruction_never_sends_another_put(large_service):
    service, table, transport = large_service
    plan = service.plan(CONTEXT, input_source(), b"x" * 33000)
    transport.responses.append(ServiceRequestError("synthetic lost upload ack"))
    with pytest.raises(Problem) as unknown:
        service.prepare_large(CONTEXT, plan)
    assert code(unknown) == "SOURCE_PREPARATION_UNKNOWN"
    transport.responses.extend([(200, blob_headers(plan))] + blob_reads(plan))
    restored = SourcePlan.model_validate_json(plan.model_dump_json())
    service.reconcile_preparation(CONTEXT, restored)
    transport.responses.extend(blob_reads(plan))
    table.failure = "after"
    with pytest.raises(Problem):
        service.commit(CONTEXT, restored)
    assert service.reconcile(CONTEXT, restored).payload["result"]["sequence"] == 1
    assert len([item for item in transport.requests if item.method == "PUT"]) == 1
    assert service.read_snapshot((CONFIG.key,))[CONFIG.key] is not None


def test_corrupt_prepared_blob_cannot_be_attached_to_new_responsibility(large_service):
    service, table, transport = large_service
    plan = prepared_large(service, transport)
    transport.responses[:] = blob_reads(plan, b"x" * len(plan.original))
    before = len(table.submissions)
    with pytest.raises(Problem) as corrupt:
        service.commit(CONTEXT, plan)
    assert code(corrupt) == "ARTIFACT_INTEGRITY_ERROR"
    assert len(table.submissions) == before
    assert service.read_snapshot((plan.reference.key,))[plan.reference.key] is None


def test_inventory_outage_is_explicit_problem_not_unverified_success():
    def unavailable():
        raise OSError("synthetic inventory outage")
    with pytest.raises(Problem) as absent:
        InventoryProtectionVerifier(unavailable, clock=lambda: NOW).verify(CONFIG)
    assert code(absent) == "SOURCE_PROTECTION_UNVERIFIED"


@pytest.mark.parametrize("failure", ["missing", "altered", "version"])
def test_missing_altered_or_repointed_blob_never_returns_empty_success(large_service, failure):
    service, _, transport = large_service
    plan = prepared_large(service, transport)
    service.commit(CONTEXT, plan)
    if failure == "missing":
        transport.responses.append((404, {"x-ms-error-code": "BlobNotFound"}))
    elif failure == "version":
        transport.responses.append((200, {**blob_headers(plan), "x-ms-version-id": "another-version"}))
    else:
        transport.responses.extend(blob_reads(plan, b"z" * len(plan.original)))
    with pytest.raises(Problem):
        service.read_exact(CONTEXT, plan.reference)
    assert len([item for item in transport.requests if item.method == "PUT"]) == 1


def test_snapshot_roundtrip_requires_complete_exact_graph_and_original_bytes(source_service):
    service, table = source_service
    leaf, _ = admitted(service, "鈷藍色\n".encode(), source_id="leaf")
    root, _ = admitted(service, b"root\x00", source_id="root", necessary=(leaf.reference,))
    scope = root.reference.scope
    receipt = service.capture_snapshot(CONTEXT, snapshot_id="checkpoint-one", scope=scope,
                                       conversation_id="conversation-one", runtime_version="runtime-v1",
                                       roots=(root.reference,))
    restored = service.restore_snapshot(CONTEXT, scope=scope, snapshot_id="checkpoint-one", receipt=receipt)
    assert restored.originals == {root.reference.key: root.original, leaf.reference.key: leaf.original}
    assert len(restored.manifest.nodes) == 2
    del table.backing.rows[("Behavior", SCOPE.partition_key, "behavior:v1:row:" + leaf.reference.key)]
    with pytest.raises(Problem) as interrupted:
        service.restore_snapshot(CONTEXT, scope=scope, snapshot_id="checkpoint-one", receipt=receipt)
    assert code(interrupted) == "SOURCE_REQUIRED_MISSING"


def test_snapshot_planning_is_read_only_and_commit_preserves_existing_capture_receipt(source_service):
    service, table = source_service
    source, _ = admitted(service)
    arguments = dict(snapshot_id="planned", scope=source.reference.scope,
                     conversation_id="conversation", runtime_version="runtime", roots=(source.reference,))
    before = len(table.submissions)
    plan = service.plan_snapshot(CONTEXT, **arguments)
    assert len(table.submissions) == before
    retained = SourceSnapshotPlan.model_validate_json(plan.model_dump_json())
    receipt = service.commit_snapshot(CONTEXT, retained)
    assert len(table.submissions[-1]) == 3  # Existing guard/receipt/manifest, no new authority.
    assert service.capture_snapshot(CONTEXT, **arguments) == receipt
    assert service.reconcile_snapshot(CONTEXT, retained) == receipt
    assert len(table.submissions) == before + 1


@pytest.mark.parametrize("failure", ["before", "after"])
def test_snapshot_unknown_reconciles_original_plan_without_rebuilding_missing_graph(source_service, failure):
    service, table = source_service
    source, _ = admitted(service)
    plan = service.plan_snapshot(CONTEXT, snapshot_id="unknown-snapshot", scope=source.reference.scope,
                                 conversation_id="conversation", runtime_version="runtime", roots=(source.reference,))
    table.failure = failure
    with pytest.raises(Problem) as unknown:
        service.commit_snapshot(CONTEXT, plan)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert unknown.value.body["error"]["request_id"] == plan.request.request_id
    del table.backing.rows[("Behavior", SCOPE.partition_key, "behavior:v1:row:" + source.reference.key)]
    restarted_table = FakeTable(table.backing)
    restarted = SourceMaterials(BehaviorStore(restarted_table, SCOPE))
    retained = SourceSnapshotPlan.model_validate_json(plan.model_dump_json())
    if failure == "after":
        receipt = restarted.reconcile_snapshot(CONTEXT, retained)
        assert receipt.payload["request"]["request_id"] == retained.request.request_id
    else:
        with pytest.raises(Problem) as absent:
            restarted.reconcile_snapshot(CONTEXT, retained)
        assert code(absent) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert restarted_table.submissions == []


def test_snapshot_plan_cannot_omit_necessary_graph_nodes_or_rebind_request(source_service):
    service, table = source_service
    leaf, _ = admitted(service, source_id="leaf")
    root, _ = admitted(service, source_id="root", necessary=(leaf.reference,))
    plan = service.plan_snapshot(CONTEXT, snapshot_id="incomplete-plan", scope=root.reference.scope,
                                 conversation_id="conversation", runtime_version="runtime", roots=(root.reference,))
    omitted = plan.manifest.model_copy(update={
        "nodes": tuple(node for node in plan.manifest.nodes if node.reference == root.reference),
    })
    request = plan.request.model_copy(update={"request_digest": digest(omitted.model_dump(mode="json"))})
    before = len(table.submissions)
    with pytest.raises(Problem) as incomplete:
        service.commit_snapshot(CONTEXT, plan.model_copy(update={"manifest": omitted, "request": request}))
    assert code(incomplete) == "SOURCE_SNAPSHOT_PLAN_MISMATCH"
    with pytest.raises(Problem) as rebound:
        service.reconcile_snapshot(CONTEXT, plan.model_copy(update={
            "request": plan.request.model_copy(update={"request_id": "different-request"}),
        }))
    assert code(rebound) == "SOURCE_SNAPSHOT_PLAN_MISMATCH"
    assert len(table.submissions) == before


def test_hash_only_incomplete_snapshot_manifest_is_not_restored_data(source_service):
    service, _ = source_service
    child, _ = admitted(service, source_id="child")
    root, _ = admitted(service, source_id="root", necessary=(child.reference,))
    receipt = service.capture_snapshot(CONTEXT, snapshot_id="complete", scope=root.reference.scope,
                                       conversation_id="c", runtime_version="r", roots=(root.reference,))
    restored = service.restore_snapshot(CONTEXT, scope=root.reference.scope, snapshot_id="complete", receipt=receipt)
    manifest = SourceSnapshot.model_validate({
        **restored.manifest.model_dump(), "snapshot_id": "incomplete",
        "nodes": tuple(node for node in restored.manifest.nodes if node.reference == root.reference),
    })
    key = control_key("source-snapshot", root.reference.scope.key, "incomplete")
    request = ControlRequest(request_id=control_key("source-snapshot-commit", key),
                             request_digest=digest(manifest.model_dump(mode="json")), read_set=(key,))
    malformed = service.store.transact(request, lambda _: ControlChanges(
        writes=(control_write(key, manifest),), result={"request": request.model_dump(mode="json")}))
    with pytest.raises(Problem) as incomplete:
        service.restore_snapshot(CONTEXT, scope=root.reference.scope, snapshot_id="incomplete", receipt=malformed)
    assert code(incomplete) == "SOURCE_GRAPH_INCOMPLETE"
