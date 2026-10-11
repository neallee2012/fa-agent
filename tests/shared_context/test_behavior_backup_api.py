"""Signed-JWT backup/recovery API tests; storage/native evidence is OFFLINE only."""

import base64
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from shared_context import behavior_wire as wire
from shared_context.behavior_api import create_behavior_app, create_source_recovery_app
from shared_context.behavior_runtime import BehaviorRuntime, CommandPlan, runtime_key
from shared_context.errors import Problem
from shared_context.source_backup import SourceBackupPlan, SourceBackupReference, SourceBackupService, make_blob_backup_store
from shared_context.source_materials import AuthenticatedSourceContext
from test_backup_store import BlobTransport, Credential
from test_behavior_api import BASE, RuntimeHarness
from test_m3_auth import HUMAN_OID, jwk_client, signing_key, token
from test_source_backup import (
    BACKUP_HUMAN, BACKUP_ID, ETAG, NATIVE_VERSION, PROTECTION, SETTINGS, CountedBackupStore, RecoveryCatalog,
    backup_fixture, native_headers, native_reads, protection_verifier, uploaded,
)
from test_source_materials import CONTEXT, admitted, source_service
from test_source_reviews import HUMAN_CONTEXT

RECOVER = "/behavior/v1/recovery/restore-quarantined"


@pytest.fixture
def h(auth, jwk_client, signing_key):
    return RuntimeHarness(auth, jwk_client, signing_key)


def headers(h, scopes):
    return {"Authorization": "Bearer " + token(h.signing, {"scp": "Behavior.Read " + scopes}),
            "Content-Type": "application/json"}


def post_backup(h, action, command, scopes="Behavior.Backup"):
    return h.client.post(BASE + f"/backups/{BACKUP_ID}/" + action,
                         content=command.model_dump_json(), headers=headers(h, scopes))


def install_backup(h, backing):
    service = SourceBackupService(
        backing, SETTINGS, sources=h.sources, protection=PROTECTION,
        protection_verifier=protection_verifier(), clock=lambda: h.time,
    )
    service.initialize_protection()
    h.dep = replace(h.dep, source_backups=service)
    h.runtime = BehaviorRuntime(h.dep, (h.binding,), clock=lambda: h.time,
                                mode="offline_contract", controller_id="internal-controller")
    h.client = TestClient(create_behavior_app(h.runtime, h.verifier))
    ref = h.source()
    assert h.post("/snapshots/snapshot/capture", wire.SnapshotCapture(request_id="snapshot", roots=(ref,))).status_code == 200
    command = wire.BackupPrepare(request_id="backup", snapshot_id="snapshot", snapshot_request_id="snapshot")
    return service, command


def saved_plan(h):
    key = runtime_key("plan", "binding-one", "backup-" + BACKUP_ID, "backup")
    row = h.store.read_snapshot((key,))[key]
    plan = CommandPlan.model_validate_json(json.dumps(row.payload["data"]))
    return SourceBackupPlan.model_validate_json(json.dumps(plan.data["backup_plan"]))


def test_backup_routes_require_separate_consent_and_reconcile_explicitly(h):
    store = CountedBackupStore()
    service, command = install_backup(h, store)
    assert post_backup(h, "prepare", command, "Behavior.Approve").status_code == 403
    assert post_backup(h, "prepare", command, "Behavior.Restore").status_code == 403
    assert h.client.post(BASE + f"/backups/{BACKUP_ID}/prepare",
                         content=command.model_dump_json(), headers=h.headers("agent")).status_code == 403
    prepared = post_backup(h, "prepare", command)
    assert prepared.status_code == 200, prepared.text
    assert post_backup(h, "prepare", command).json() == prepared.json()
    accepted = post_backup(h, "upload", wire.Command(request_id="backup"))
    assert accepted.status_code == 200, accepted.text
    reference = SourceBackupReference.model_validate(accepted.json()["result"]["reference"])
    assert reference.description.byte_length == prepared.json()["archive_bytes"]
    assert store.creates == 1
    assert post_backup(h, "upload", wire.Command(request_id="backup")).status_code == 409
    reconciled = post_backup(h, "reconcile", wire.Command(request_id="backup"))
    assert reconciled.status_code == 200 and reconciled.json() == accepted.json()
    assert store.creates == 1


@pytest.mark.parametrize("phase", ["before", "after"])
def test_upload_unknown_never_recreates_blob_and_requires_original_plan(h, phase):
    backing = CountedBackupStore()
    _, command = install_backup(h, backing)
    assert post_backup(h, "prepare", command).status_code == 200
    backing.fail_create = phase
    unknown = post_backup(h, "upload", wire.Command(request_id="backup"))
    assert unknown.status_code == 503 and unknown.json()["error"]["reconcile_original"]
    assert backing.creates == 1
    assert post_backup(h, "upload", wire.Command(request_id="backup")).status_code == 409
    backing.fail_create = None
    reconciled = post_backup(h, "reconcile", wire.Command(request_id="backup"))
    assert reconciled.status_code == (200 if phase == "after" else 503)
    assert backing.creates == 1


def test_real_blob_sdk_backup_then_independent_recovery_after_all_source_rows_removed(h):
    transport = BlobTransport([])
    backing = make_blob_backup_store(SETTINGS, Credential(), transport=transport)
    try:
        service, command = install_backup(h, backing)
        assert post_backup(h, "prepare", command).status_code == 200
        plan = saved_plan(h)
        context = AuthenticatedSourceContext(h.binding.tenant_id, h.binding.team_id, "human-" + HUMAN_OID,
                                            h.binding.ingress_id, ("metrics",))
        restored = h.sources.restore_snapshot(context, scope=plan.manifest.scope,
                                              snapshot_id=plan.manifest.snapshot_id,
                                              receipt=plan.snapshot_receipt.record())
        _, archive = service._archive(BACKUP_ID, HUMAN_OID, restored)
        transport.responses.extend([(201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}),
                                    (200, native_headers(plan)), *native_reads(plan, archive)])
        uploaded_response = post_backup(h, "upload", wire.Command(request_id="backup"))
        assert uploaded_response.status_code == 200, uploaded_response.text
        reference = SourceBackupReference.model_validate(uploaded_response.json()["result"]["reference"])
        catalog = RecoveryCatalog()
        catalog.permit(reference)
        independent = SourceBackupService(backing, SETTINGS, recovery_authorizer=catalog, clock=lambda: h.time)
        recovery = TestClient(create_source_recovery_app(independent, h.verifier))
        h.transport.backing.rows.clear()
        transport.responses.extend(native_reads(plan, archive))
        request = wire.QuarantineRestore(request_id="independent-restore", reference=reference.model_dump(mode="json"))
        response = recovery.post(RECOVER, content=request.model_dump_json(), headers=headers(h, "Behavior.Restore"))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "quarantined" and response.json()["activation_performed"] is False
        assert {key: base64.b64decode(value) for key, value in response.json()["originals_base64"].items()} == restored.originals
        assert not h.transport.backing.rows
        assert len([request for request in transport.requests if request.method == "PUT"]) == 1
        assert all(request.headers["If-Match"] == ETAG for request in transport.requests if request.method == "GET")
    finally:
        backing.container.close()


def test_restore_is_freshly_authorized_privileged_binary_transfer_not_archive_acl(h, backup_fixture):
    service, _, table, backing, catalog, plan, originals = backup_fixture
    reference, _ = uploaded(service, plan)
    independent = SourceBackupService(backing, SETTINGS, recovery_authorizer=catalog, clock=lambda: h.time)
    recovery = TestClient(create_source_recovery_app(independent, h.verifier))
    body = wire.QuarantineRestore(request_id="restore", reference=reference.model_dump(mode="json"))
    for scope in ("", "Behavior.Approve", "Behavior.Backup"):
        assert recovery.post(RECOVER, content=body.model_dump_json(), headers=headers(h, scope)).status_code == 403
    assert recovery.post(RECOVER, content=body.model_dump_json(), headers=h.headers("agent")).status_code == 403
    # Even the correct delegated scope cannot grant access to an unregistered reference.
    assert recovery.post(RECOVER, content=body.model_dump_json(), headers=headers(h, "Behavior.Restore")).status_code == 403
    catalog.permit(reference)
    table.backing.rows.clear()
    response = recovery.post(RECOVER, content=body.model_dump_json(), headers=headers(h, "Behavior.Restore"))
    assert response.status_code == 200
    assert {key: base64.b64decode(value) for key, value in response.json()["originals_base64"].items()} == originals
    catalog.decisions.clear()
    assert recovery.post(RECOVER, content=body.model_dump_json(), headers=headers(h, "Behavior.Restore")).status_code == 403
    assert recovery.post("/behavior/v1/recovery/activate", content="{}", headers=headers(h, "Behavior.Restore")).status_code == 404
    assert not table.backing.rows


def test_recovery_does_not_accept_json_authorization_or_use_live_runtime(h, backup_fixture):
    service, _, _, backing, catalog, plan, _ = backup_fixture
    reference, _ = uploaded(service, plan)
    with pytest.raises(Problem):
        create_source_recovery_app(service, h.verifier)
    with pytest.raises(Problem):
        create_source_recovery_app(SourceBackupService(backing, SETTINGS, clock=lambda: h.time), h.verifier)
    independent = SourceBackupService(backing, SETTINGS, recovery_authorizer=catalog, clock=lambda: h.time)
    recovery = TestClient(create_source_recovery_app(independent, h.verifier))
    body = {"request_id": "restore", "reference": reference.model_dump(mode="json")}
    for field in ("approved", "authorization", "context", "clock", "activate"):
        response = recovery.post(RECOVER, content=json.dumps({**body, field: True}), headers=headers(h, "Behavior.Restore"))
        assert response.status_code == 422 and "originals_base64" not in response.text


def test_quarantine_response_keeps_all_binary_bytes_above_normal_api_response_limit(h, backup_fixture):
    service, sources, table, backing, catalog, _, _ = backup_fixture
    originals, references = {}, []
    for number in range(6):
        raw = bytes([number]) * (32 * 1024)
        source, _ = admitted(sources, raw, source_id=f"larger-{number}")
        originals[source.reference.key] = raw
        references.append(source.reference)
    root, _ = admitted(sources, b"complete large archive", source_id="larger-root", necessary=tuple(references))
    originals[root.reference.key] = root.original
    snapshot = sources.capture_snapshot(
        CONTEXT, snapshot_id="larger", scope=root.reference.scope,
        conversation_id="conversation", runtime_version="runtime", roots=(root.reference,),
    )
    plan = service.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=root.reference.scope,
                        snapshot_id="larger", snapshot_receipt=snapshot)
    reference, _ = uploaded(service, plan)
    catalog.permit(reference)
    table.backing.rows.clear()
    independent = SourceBackupService(backing, SETTINGS, recovery_authorizer=catalog, clock=lambda: h.time)
    client = TestClient(create_source_recovery_app(independent, h.verifier))
    response = client.post(RECOVER, headers=headers(h, "Behavior.Restore"),
                           content=wire.QuarantineRestore(request_id="large", reference=reference.model_dump(mode="json")).model_dump_json())
    assert response.status_code == 200 and len(response.content) > 128 * 1024
    assert {key: base64.b64decode(value) for key, value in response.json()["originals_base64"].items()} == originals
    assert not table.backing.rows


def test_recovery_revocation_during_download_does_not_release_partial_or_complete_data(h, backup_fixture):
    service, _, _, backing, catalog, plan, _ = backup_fixture
    reference, _ = uploaded(service, plan)
    catalog.permit(reference)
    download = backing.download

    def revoked(*args):
        result = download(*args)
        catalog.decisions.clear()
        return result

    backing.download = revoked
    independent = SourceBackupService(backing, SETTINGS, recovery_authorizer=catalog, clock=lambda: h.time)
    client = TestClient(create_source_recovery_app(independent, h.verifier))
    response = client.post(RECOVER, headers=headers(h, "Behavior.Restore"),
                           content=wire.QuarantineRestore(request_id="revoked", reference=reference.model_dump(mode="json")).model_dump_json())
    assert response.status_code == 403 and "originals_base64" not in response.text


def test_incomplete_protection_inventory_never_enables_backup(h):
    reference = h.source()
    assert h.post("/snapshots/snapshot/capture", wire.SnapshotCapture(
        request_id="snapshot", roots=(reference,),
    )).status_code == 200
    backing = CountedBackupStore()
    service = SourceBackupService(
        backing, SETTINGS, sources=h.sources, protection=PROTECTION,
        protection_verifier=None, clock=lambda: h.time,
    )
    deps = replace(h.dep, source_backups=service)
    runtime = BehaviorRuntime(deps, (h.binding,), clock=lambda: h.time, mode="offline_contract")
    client = TestClient(create_behavior_app(runtime, h.verifier))
    response = client.post(BASE + f"/backups/{BACKUP_ID}/prepare",
                           content=wire.BackupPrepare(request_id="x", snapshot_id="snapshot",
                                                     snapshot_request_id="snapshot").model_dump_json(),
                           headers=headers(h, "Behavior.Backup"))
    assert response.status_code == 503 and backing.creates == 0
