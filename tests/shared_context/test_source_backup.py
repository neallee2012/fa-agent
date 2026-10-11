"""Independent fake storage and real SDK/local transport; no Azure/DR claims."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import replace
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from azure.core.exceptions import ServiceRequestError
from azure.storage.blob import ContainerClient

from conftest import TENANT
from test_backup_store import BlobTransport, Credential
from test_behavior_store import FakeTable, SCOPE
from test_source_materials import CONTEXT, NOW, admitted, code, native_state, source_service
from test_source_reviews import AGENT, HUMAN, HUMAN_CONTEXT
from shared_context.backup_models import MAX_BACKUP_BYTES
from shared_context.backup_store import BlobBackupStore, InMemoryBackupStore
from shared_context.behavior_store import BehaviorStore, ControlChanges, ControlRequest
from shared_context.source_materials import (
    InventoryProtectionVerifier, NoAutoGCConfiguration, SnapshotNode, SourceMaterials, SourceReference,
    SourceSnapshot, canonical, control_key, control_write, digest,
)
from shared_context.source_backup import (
    MAGIC, MAX_HEADER_BYTES, ArchiveSegment, RecoveryAuthorization, SourceArchiveHeader,
    SourceBackupPlan, SourceBackupReference, SourceBackupService, SourceBackupSettings,
    decode_archive, encode_archive, make_blob_backup_store,
)
from shared_context.errors import Problem

BACKUP_HUMAN = replace(HUMAN, actions=HUMAN.actions | frozenset({"backup", "restore"}))
BACKUP_ID = "0123456789abcdef0123456789abcdef"
SETTINGS = SourceBackupSettings(tenant_id=TENANT, authority_domain=SCOPE.authority_domain,
                                container_url="https://synthetic.blob.core.windows.net/source-backups")
PROTECTION = NoAutoGCConfiguration(
    authority_domain=SCOPE.authority_domain, generation="backup-g1", container_url=SETTINGS.container_url,
    protected_prefix=f"v1/{TENANT}/", configuration_version="backup-config-v1", evidence_sha256="b" * 64,
)


def protection_verifier(**changes):
    state = native_state(generation=PROTECTION.generation, container_url=PROTECTION.container_url,
                         protected_prefix=PROTECTION.protected_prefix,
                         configuration_version=PROTECTION.configuration_version,
                         evidence_sha256=PROTECTION.evidence_sha256)
    return InventoryProtectionVerifier(lambda: replace(state, **changes), clock=lambda: NOW)


class RecoveryCatalog:
    """Trusted synthetic recovery decisions, kept independently of source rows."""
    def __init__(self):
        self.decisions = {}
        self.calls = 0
        self.failure = None

    def permit(self, reference):
        proof = RecoveryAuthorization(
            decision_id="explicit-recovery-one", policy_version="recovery-policy-v1",
            principal_oid=BACKUP_HUMAN.oid, reference_sha256=reference.sha256,
            scope=reference.description.scope, current_access_revision=7, expires_at=NOW + timedelta(hours=1),
        )
        self.decisions[(BACKUP_HUMAN.oid, reference.sha256)] = proof
        return proof

    def authorize(self, principal, reference):
        self.calls += 1
        if self.failure:
            raise self.failure
        proof = self.decisions.get((principal.oid, reference.sha256))
        if proof is None:
            raise Problem(403, "TEST_RECOVERY_DENIED", "no current independent recovery decision")
        return proof


class CountedBackupStore(InMemoryBackupStore):
    def __init__(self):
        super().__init__(TENANT)
        self.creates, self.downloads = 0, 0
        self.fail_create = None
        self.after_create = None

    def create(self, *args):
        self.creates += 1
        if self.fail_create == "before":
            raise Problem(503, "TEST_UPLOAD_LOST", "synthetic upload not observed")
        result = super().create(*args)
        if self.after_create:
            callback, self.after_create = self.after_create, None
            callback()
        if self.fail_create == "after":
            raise Problem(503, "TEST_UPLOAD_LOST", "synthetic accepted upload acknowledgement lost")
        return result

    def download(self, *args):
        self.downloads += 1
        return super().download(*args)


def service(sources, backups, catalog=None, **changes):
    return SourceBackupService(
        backups, SETTINGS, sources=sources, protection=PROTECTION,
        protection_verifier=protection_verifier(), recovery_authorizer=catalog, clock=lambda: NOW, **changes,
    )


def snapshot(sources):
    leaf, _ = admitted(sources, "中文\r\n完整原文\n".encode() + bytes(range(256)), source_id="leaf")
    root, _ = admitted(sources, b'{"key": 1, "key": 2}\r\n\x00', source_id="root", necessary=(leaf.reference,))
    receipt = sources.capture_snapshot(CONTEXT, snapshot_id="checkpoint", scope=root.reference.scope,
                                       conversation_id="conversation", runtime_version="runtime",
                                       roots=(root.reference,))
    return root.reference.scope, receipt, {leaf.reference.key: leaf.original, root.reference.key: root.original}


@pytest.fixture
def backup_fixture(source_service):
    sources, table = source_service
    scope, receipt, originals = snapshot(sources)
    backups, catalog = CountedBackupStore(), RecoveryCatalog()
    api = service(sources, backups, catalog)
    api.initialize_protection()
    plan = api.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=scope,
                    snapshot_id="checkpoint", snapshot_receipt=receipt)
    return api, sources, table, backups, catalog, plan, originals


def uploaded(api, plan):
    receipt = api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    return SourceBackupReference.model_validate(receipt.payload["result"]["reference"]), receipt


def test_archive_preserves_exact_bytes_and_backup_recovers_after_entire_source_store_is_removed(backup_fixture):
    api, _, table, backups, catalog, plan, originals = backup_fixture
    reference, completion = uploaded(api, plan)
    catalog.permit(reference)
    assert backups.creates == 1
    table.backing.rows.clear()
    recovery_only = SourceBackupService(backups, SETTINGS, recovery_authorizer=catalog, clock=lambda: NOW)
    result = recovery_only.restore_quarantined(BACKUP_HUMAN, reference)
    assert result.status == "quarantined" and result.originals == originals
    assert result.manifest == plan.manifest
    assert not table.backing.rows  # No guard/claim/budget/owner/job activation or restore.
    assert completion.payload["result"]["reference"]["blob"]["version_id"] == reference.blob.version_id
    assert not hasattr(result, "activate") and not hasattr(recovery_only, "restore_control_state")


def test_plan_is_deterministic_and_untrusted_self_consistent_hash_is_checked_against_real_snapshot(backup_fixture):
    api, sources, table, backups, _, plan, _ = backup_fixture
    again = api.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=plan.description.scope,
                     snapshot_id="checkpoint", snapshot_receipt=plan.snapshot_receipt.record())
    assert again == plan
    fake = plan.description.model_copy(update={"archive_sha256": "f" * 64})
    forged = plan.model_copy(update={
        "description": fake,
        "request": ControlRequest(request_id=plan.request.request_id, request_digest=digest(fake.model_dump(mode="json")),
                                  read_set=plan.request.read_set),
    })
    before = len(table.submissions)
    with pytest.raises(Problem) as invalid:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, forged)
    assert code(invalid) == "SOURCE_BACKUP_INTEGRITY_ERROR"
    assert backups.creates == 0 and len(table.submissions) == before
    missing = plan.manifest.nodes[0].reference.key
    del table.backing.rows[("Behavior", SCOPE.partition_key, "behavior:v1:row:" + missing)]
    with pytest.raises(Problem):
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert backups.creates == 0


@pytest.mark.parametrize("phase", ["before", "after"])
def test_unknown_upload_is_single_consumed_and_only_explicitly_reconciled(backup_fixture, phase):
    api, sources, table, backups, catalog, plan, originals = backup_fixture
    backups.fail_create = phase
    with pytest.raises(Problem) as unknown:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(unknown) == "SOURCE_BACKUP_UPLOAD_UNKNOWN"
    assert unknown.value.body["error"]["request_id"] == plan.request.request_id
    with pytest.raises(Problem) as repeated:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(repeated) == "SOURCE_BACKUP_RECONCILE_REQUIRED"
    assert backups.creates == 1
    rebuilt_sources = SourceMaterials(BehaviorStore(FakeTable(table.backing), SCOPE))
    rebuilt = service(rebuilt_sources, backups, catalog)
    retained = SourceBackupPlan.model_validate_json(plan.model_dump_json())
    if phase == "after":
        receipt = rebuilt.reconcile_upload(BACKUP_HUMAN, HUMAN_CONTEXT, retained)
        reference = SourceBackupReference.model_validate(receipt.payload["result"]["reference"])
        catalog.permit(reference)
        table.backing.rows.clear()
        assert rebuilt.restore_quarantined(BACKUP_HUMAN, reference).originals == originals
    else:
        with pytest.raises(Problem) as absent:
            rebuilt.reconcile_upload(BACKUP_HUMAN, HUMAN_CONTEXT, retained)
        assert code(absent) == "SOURCE_BACKUP_UPLOAD_UNKNOWN"
    assert backups.creates == 1


def test_crash_after_intent_before_blob_and_unknown_control_ack_never_recreate_upload(backup_fixture):
    api, _, table, backups, _, plan, _ = backup_fixture
    table.failure = "after"
    with pytest.raises(Problem) as unknown:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    assert backups.creates == 0
    table.failure = None
    with pytest.raises(Problem) as absent:
        api.reconcile_upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(absent) == "SOURCE_BACKUP_UPLOAD_UNKNOWN"
    with pytest.raises(Problem) as repeated:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(repeated) == "SOURCE_BACKUP_RECONCILE_REQUIRED"
    assert backups.creates == 0


def test_unknown_completion_ack_reconciles_exact_version_without_second_upload(backup_fixture):
    api, _, table, backups, _, plan, _ = backup_fixture
    backups.after_create = lambda: setattr(table, "failure", "after")
    with pytest.raises(Problem) as unknown:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(unknown) == "BEHAVIOR_COMMIT_UNKNOWN"
    table.failure = None
    first = api.reconcile_upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    second = api.reconcile_upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert first == second and backups.creates == 1


def test_generation_change_after_upload_prevents_unconditional_completion(backup_fixture):
    api, _, table, backups, _, plan, _ = backup_fixture
    other = BehaviorStore(FakeTable(table.backing), SCOPE)
    key = control_key("source-backup-protection", SETTINGS.authority_domain, SETTINGS.container_url)
    changed = PROTECTION.model_copy(update={"generation": "retired-generation"})
    backups.after_create = lambda: other.transact(
        ControlRequest(request_id="retire-backup-generation", request_digest="d" * 64, read_set=(key,)),
        lambda _: ControlChanges(writes=(control_write(key, changed, "replace"),), result={}))
    with pytest.raises(Problem) as stale:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(stale) == "SOURCE_BACKUP_PROTECTION_CHANGED"
    assert backups.creates == 1
    completion_key = control_key("source-backup-completion", TENANT, HUMAN.oid, BACKUP_ID)
    assert other.read_snapshot((completion_key,))[completion_key] is None


@pytest.mark.parametrize("principal", [HUMAN, AGENT, replace(AGENT, actions=frozenset({"read", "backup", "restore"}))],
                         ids=["read-approve-human", "agent", "agent-forged-actions"])
def test_backup_restore_actions_are_not_inherited_from_read_approve_or_agent_roles(backup_fixture, principal):
    api, _, _, backups, catalog, plan, _ = backup_fixture
    with pytest.raises(Problem):
        api.upload(principal, HUMAN_CONTEXT, plan)
    assert backups.creates == 0
    reference, _ = uploaded(api, plan)
    catalog.permit(reference)
    before = backups.downloads
    with pytest.raises(Problem):
        api.restore_quarantined(principal, reference)
    assert backups.downloads == before


def test_recovery_needs_current_independent_authorization_not_archive_acl_or_http_boolean(backup_fixture):
    api, _, _, backups, catalog, plan, _ = backup_fixture
    reference, _ = uploaded(api, plan)
    recovery = SourceBackupService(backups, SETTINGS, clock=lambda: NOW)
    before = backups.downloads
    with pytest.raises(Problem) as missing:
        recovery.restore_quarantined(BACKUP_HUMAN, reference)
    assert code(missing) == "SOURCE_BACKUP_RECOVERY_DENIED"
    with pytest.raises(Problem) as unknown:
        api.restore_quarantined(BACKUP_HUMAN, reference)
    assert code(unknown) == "TEST_RECOVERY_DENIED"
    catalog.decisions[(HUMAN.oid, reference.sha256)] = True
    with pytest.raises(Problem) as boolean:
        api.restore_quarantined(BACKUP_HUMAN, reference)
    assert code(boolean) == "SOURCE_BACKUP_RECOVERY_DENIED"
    assert backups.downloads == before
    catalog.failure = OSError("current recovery policy unavailable")
    with pytest.raises(Problem) as unavailable:
        api.restore_quarantined(BACKUP_HUMAN, reference)
    assert code(unavailable) == "SOURCE_BACKUP_RECOVERY_UNKNOWN"


def test_recovery_denies_expired_wrong_owner_scope_hash_size_and_native_version(backup_fixture):
    api, _, _, backups, catalog, plan, _ = backup_fixture
    reference, _ = uploaded(api, plan)
    proof = catalog.permit(reference)
    wrong_owner = replace(BACKUP_HUMAN, oid="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    with pytest.raises(Problem) as owner:
        api.restore_quarantined(wrong_owner, reference)
    assert code(owner) == "SOURCE_BACKUP_SCOPE_DENIED"
    catalog.decisions[(HUMAN.oid, reference.sha256)] = proof.model_copy(update={"expires_at": NOW})
    with pytest.raises(Problem):
        api.restore_quarantined(BACKUP_HUMAN, reference)
    catalog.permit(reference)
    variants = [
        reference.model_copy(update={"description": reference.description.model_copy(update={
            "scope": reference.description.scope.model_copy(update={"team_id": "other-team"})})}),
        reference.model_copy(update={"description": reference.description.model_copy(update={"archive_sha256": "f" * 64})}),
        reference.model_copy(update={"description": reference.description.model_copy(update={"byte_length": 1})}),
        reference.model_copy(update={"blob": reference.blob.model_copy(update={"version_id": "different-version"})}),
    ]
    for changed in variants:
        with pytest.raises(Problem):
            api.restore_quarantined(BACKUP_HUMAN, changed)
    # Even an explicit catalog decision for a nonexistent native version cannot
    # replace that version with the current alias.
    catalog.permit(variants[-1])
    with pytest.raises(Problem) as version:
        api.restore_quarantined(BACKUP_HUMAN, variants[-1])
    assert code(version) == "BACKUP_NOT_FOUND"


def test_current_recovery_access_change_during_download_never_returns_quarantined_bytes(backup_fixture):
    api, _, _, backups, catalog, plan, _ = backup_fixture
    reference, _ = uploaded(api, plan)
    catalog.permit(reference)
    original = backups.download
    def withdraw_after_read(*args):
        result = original(*args)
        catalog.decisions.clear()
        return result
    backups.download = withdraw_after_read
    with pytest.raises(Problem):
        api.restore_quarantined(BACKUP_HUMAN, reference)


@pytest.mark.parametrize("failure", ["missing", "corrupt", "size", "version", "owner"])
def test_missing_or_corrupt_independent_backup_never_returns_partial_recovery(backup_fixture, failure):
    api, _, _, backups, catalog, plan, _ = backup_fixture
    reference, _ = uploaded(api, plan)
    catalog.permit(reference)
    key = HUMAN.oid, BACKUP_ID
    receipt, data = backups._backups[key]
    if failure == "missing":
        del backups._backups[key]
    elif failure == "corrupt":
        backups._backups[key] = receipt, data[:-1] + bytes([data[-1] ^ 1])
    else:
        updates = {
            "size": {"bytes": 1}, "version": {"version_id": "different-version"},
            "owner": {"owner_oid": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"},
        }[failure]
        backups._backups[key] = receipt.model_copy(update=updates), data
    with pytest.raises(Problem):
        api.restore_quarantined(BACKUP_HUMAN, reference)


def test_no_protection_or_unsafe_native_inventory_never_uploads(backup_fixture):
    api, sources, _, backups, _, plan, _ = backup_fixture
    disabled = SourceBackupService(backups, SETTINGS, sources=sources, clock=lambda: NOW)
    with pytest.raises(Problem) as missing:
        disabled.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=plan.description.scope,
                      snapshot_id="checkpoint", snapshot_receipt=plan.snapshot_receipt.record())
    assert code(missing) == "SOURCE_BACKUP_PROTECTION_UNVERIFIED"
    api._verifier = protection_verifier(automatic_delete_consumers=("backup-expiry-worker",))
    with pytest.raises(Problem) as unsafe:
        api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
    assert code(unsafe) == "SOURCE_PROTECTION_UNVERIFIED"
    assert backups.creates == 0


def archive_from_plan(api, plan):
    restored = api.sources.restore_snapshot(HUMAN_CONTEXT, scope=plan.description.scope, snapshot_id="checkpoint",
                                            receipt=plan.snapshot_receipt.record())
    header = SourceArchiveHeader(
        backup_id=BACKUP_ID, owner_oid=HUMAN.oid, manifest=plan.manifest,
        snapshot_receipt_sha256=plan.description.snapshot_receipt_sha256,
        authority_binding=restored.receipt.payload["binding"], guard_revision=restored.receipt.payload["guard_revision"],
        segments=tuple(ArchiveSegment(key=node.reference.key, byte_length=node.reference.byte_length,
                                      sha256=node.reference.sha256)
                       for node in sorted(plan.manifest.nodes, key=lambda node: node.reference.key)))
    return header, encode_archive(header, restored.originals)


def raw_archive(header_data, trailing=b""):
    raw = canonical(header_data)
    return MAGIC + struct.pack(">I", len(raw)) + raw + trailing


def test_archive_rejects_corruption_missing_duplicate_inconsistent_entries_and_paths(backup_fixture):
    api, _, _, _, _, plan, originals = backup_fixture
    header, data = archive_from_plan(api, plan)
    assert decode_archive(data).originals == originals
    for invalid in (data[:-1], data + b"extra", data[:-1] + bytes([data[-1] ^ 0xFF]), b"PK\x03\x04zip",
                    MAGIC + struct.pack(">I", MAX_HEADER_BYTES + 1), MAGIC + struct.pack(">I", 100) + b"{}"):
        with pytest.raises(Problem):
            decode_archive(invalid)
    value = header.model_dump(mode="json")
    variants = []
    duplicate = json.loads(json.dumps(value))
    duplicate["segments"].append(duplicate["segments"][0])
    variants.append(duplicate)
    missing = json.loads(json.dumps(value))
    missing["manifest"]["nodes"] = missing["manifest"]["nodes"][:1]
    variants.append(missing)
    traversal = json.loads(json.dumps(value))
    traversal["segments"][0]["key"] = "../../outside"
    variants.append(traversal)
    fake_hash = json.loads(json.dumps(value))
    fake_hash["segments"][0]["sha256"] = "f" * 64
    variants.append(fake_hash)
    activation = json.loads(json.dumps(value))
    activation["activate_claims"] = True
    variants.append(activation)
    for variant in variants:
        with pytest.raises(Problem):
            decode_archive(raw_archive(variant))
    raw = canonical(value)
    duplicate_json = b'{"backup_id":"' + BACKUP_ID.encode() + b'",' + raw[1:]
    with pytest.raises(Problem) as duplicate_field:
        decode_archive(MAGIC + struct.pack(">I", len(duplicate_json)) + duplicate_json)
    assert code(duplicate_field) == "SOURCE_BACKUP_FORMAT_INVALID"


def test_eight_mib_total_bound_includes_metadata_and_never_truncates():
    scope = CONTEXT.scope("team")
    sizes = [512 * 1024] * 16
    for _ in range(8):
        originals, nodes = {}, []
        for index, size in enumerate(sizes):
            data = bytes([index]) * size
            reference = SourceReference(scope=scope, source_id=f"source-{index}", version=1,
                                        sha256=hashlib.sha256(data).hexdigest(), byte_length=size)
            originals[reference.key] = data
            nodes.append(SnapshotNode(reference=reference, sequence=index + 1, necessary=()))
        manifest = SourceSnapshot(snapshot_id="boundary", scope=scope, conversation_id="conversation",
                                  runtime_version="runtime", roots=tuple(node.reference for node in nodes),
                                  nodes=tuple(nodes), covered_sequence=16)
        header = SourceArchiveHeader(
            backup_id=BACKUP_ID, owner_oid=HUMAN.oid, manifest=manifest,
            snapshot_receipt_sha256="a" * 64, authority_binding="b" * 64, guard_revision=1,
            segments=tuple(ArchiveSegment(key=node.reference.key, sha256=node.reference.sha256,
                                          byte_length=node.reference.byte_length)
                           for node in sorted(nodes, key=lambda node: node.reference.key)))
        total = len(MAGIC) + 4 + len(canonical(header.model_dump(mode="json"))) + sum(sizes)
        if total == MAX_BACKUP_BYTES:
            break
        if sizes[-1] == 512 * 1024:
            with pytest.raises(Problem) as too_large:
                encode_archive(header, originals)
            assert code(too_large) == "SOURCE_BACKUP_TOO_LARGE"
        sizes[-1] -= total - MAX_BACKUP_BYTES
    assert total == MAX_BACKUP_BYTES
    archive = encode_archive(header, originals)
    assert len(archive) == MAX_BACKUP_BYTES
    assert decode_archive(archive).originals == originals
    with pytest.raises(Problem) as extra:
        decode_archive(archive + b"x")
    assert code(extra) == "SOURCE_BACKUP_TOO_LARGE"


def test_actual_oversized_header_is_rejected_even_with_tiny_original_data():
    scope, nodes, originals = CONTEXT.scope("team"), [], {}
    for index in range(24):
        data = bytes([index])
        reference = SourceReference(scope=scope, source_id=f"header-{index}", version=1,
                                    sha256=hashlib.sha256(data).hexdigest(), byte_length=1)
        originals[reference.key] = data
        nodes.append(SnapshotNode(reference=reference, sequence=index + 1,
                                  necessary=tuple(node.reference for node in nodes)))
    manifest = SourceSnapshot(snapshot_id="large-header", scope=scope, conversation_id="conversation",
                              runtime_version="runtime", roots=(nodes[-1].reference,),
                              nodes=tuple(nodes), covered_sequence=24)
    header = SourceArchiveHeader(
        backup_id=BACKUP_ID, owner_oid=HUMAN.oid, manifest=manifest,
        snapshot_receipt_sha256="a" * 64, authority_binding="b" * 64, guard_revision=1,
        segments=tuple(ArchiveSegment(key=node.reference.key, byte_length=1, sha256=node.reference.sha256)
                       for node in sorted(nodes, key=lambda node: node.reference.key)))
    assert len(canonical(header.model_dump(mode="json"))) > MAX_HEADER_BYTES
    with pytest.raises(Problem) as oversized:
        encode_archive(header, originals)
    assert code(oversized) == "SOURCE_BACKUP_HEADER_TOO_LARGE"


def test_independent_backup_is_not_limited_by_the_m3_single_material_size(source_service):
    sources, _ = source_service
    leaves = [admitted(sources, bytes([i]) * (32 * 1024), source_id=f"leaf-{i}")[0] for i in range(17)]
    root, _ = admitted(sources, b"root", source_id="root", necessary=tuple(leaf.reference for leaf in leaves))
    receipt = sources.capture_snapshot(CONTEXT, snapshot_id="checkpoint", scope=root.reference.scope,
                                       conversation_id="conversation", runtime_version="runtime",
                                       roots=(root.reference,))
    backups = CountedBackupStore()
    api = service(sources, backups)
    api.initialize_protection()
    plan = api.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=root.reference.scope,
                    snapshot_id="checkpoint", snapshot_receipt=receipt)
    assert 512 * 1024 < plan.description.byte_length < MAX_BACKUP_BYTES
    uploaded(api, plan)
    assert backups.creates == 1


NATIVE_VERSION = "2026-09-15T15:00:00.0000000Z"
ETAG = '"0xSOURCEBACKUP"'


def native_headers(plan):
    return {
        "ETag": ETAG, "x-ms-version-id": NATIVE_VERSION, "Content-Length": str(plan.description.byte_length),
        "Content-Type": "application/octet-stream", "x-ms-blob-type": "BlockBlob",
        "Last-Modified": "Tue, 15 Sep 2026 15:00:00 GMT",
        "x-ms-meta-tenant_id": TENANT, "x-ms-meta-owner_oid": HUMAN.oid, "x-ms-meta-backup_id": BACKUP_ID,
        "x-ms-meta-sha256": plan.description.archive_sha256, "x-ms-meta-bytes": str(plan.description.byte_length),
    }


def native_reads(plan, archive):
    headers = native_headers(plan)
    return [(200, headers), (206, {**headers, "Content-Range": f"bytes 0-{len(archive)-1}/{len(archive)}"}, archive)]


def test_real_sdk_independent_blob_upload_and_recovery_without_original_authority(source_service):
    sources, table = source_service
    scope, snapshot_receipt, originals = snapshot(sources)
    transport, catalog = BlobTransport([]), RecoveryCatalog()
    backing = make_blob_backup_store(SETTINGS, Credential(), transport=transport)
    assert transport.requests == []
    with backing.container:
        api = service(sources, backing, catalog)
        api.initialize_protection()
        plan = api.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=scope,
                        snapshot_id="checkpoint", snapshot_receipt=snapshot_receipt)
        _, archive = archive_from_plan(api, plan)
        transport.responses.extend([(201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}),
                                    (200, native_headers(plan))] + native_reads(plan, archive))
        reference, _ = uploaded(api, plan)
        catalog.permit(reference)
        table.backing.rows.clear()
        recovery = SourceBackupService(backing, SETTINGS, recovery_authorizer=catalog, clock=lambda: NOW)
        transport.responses.extend(native_reads(plan, archive))
        restored = recovery.restore_quarantined(BACKUP_HUMAN, reference)
        assert restored.originals == originals and restored.status == "quarantined"
        put = transport.requests[0]
        assert put.method == "PUT" and put.body == archive and put.headers["If-None-Match"] == "*"
        assert unquote(urlsplit(put.url).path) == f"/source-backups/v1/{TENANT}/{HUMAN.oid}/{BACKUP_ID}"
        assert len([item for item in transport.requests if item.method == "PUT"]) == 1
        for request in transport.requests[1:]:
            assert parse_qs(urlsplit(request.url).query)["versionid"] == [NATIVE_VERSION]
            if request.method == "GET":
                assert request.headers["If-Match"] == ETAG
        assert not table.backing.rows


def test_real_sdk_unknown_upload_reconciliation_never_sends_a_second_put(source_service):
    sources, _ = source_service
    scope, receipt, _ = snapshot(sources)
    transport = BlobTransport([])
    with ContainerClient("https://synthetic.blob.core.windows.net", "source-backups", credential=Credential(),
                         transport=transport, retry_total=0, max_single_put_size=MAX_BACKUP_BYTES,
                         max_single_get_size=MAX_BACKUP_BYTES) as container:
        api = service(sources, BlobBackupStore(container, TENANT))
        api.initialize_protection()
        plan = api.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=scope,
                        snapshot_id="checkpoint", snapshot_receipt=receipt)
        _, archive = archive_from_plan(api, plan)
        transport.responses.append(ServiceRequestError("synthetic lost PUT acknowledgement"))
        with pytest.raises(Problem) as unknown:
            api.upload(BACKUP_HUMAN, HUMAN_CONTEXT, plan)
        assert code(unknown) == "SOURCE_BACKUP_UPLOAD_UNKNOWN"
        transport.responses.extend([(200, native_headers(plan))] + native_reads(plan, archive))
        result = api.reconcile_upload(BACKUP_HUMAN, HUMAN_CONTEXT, SourceBackupPlan.model_validate_json(plan.model_dump_json()))
        assert result.payload["result"]["reference"]["blob"]["version_id"] == NATIVE_VERSION
        assert len([request for request in transport.requests if request.method == "PUT"]) == 1
