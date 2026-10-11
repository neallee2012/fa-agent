"""Independent quarantine authorization outlives source applicability, offline only."""

from datetime import timedelta

import pytest

from shared_context.errors import Problem
from shared_context.source_backup import SourceBackupService
from test_source_backup import (
    BACKUP_HUMAN, BACKUP_ID, PROTECTION, SETTINGS, CountedBackupStore, RecoveryCatalog, protection_verifier, uploaded,
)
from test_source_expiry import expiring_service
from test_source_materials import CONTEXT, code, input_source
from test_source_reviews import HUMAN_CONTEXT


def test_expired_source_remains_recoverable_only_through_independent_quarantine_authorization():
    sources, _, clock = expiring_service()
    expiry = clock.value + timedelta(seconds=1)
    original = b"retained despite applicability expiry"
    source = sources.plan(CONTEXT, input_source(expires_at=expiry), original)
    sources.commit(CONTEXT, source)
    scope = source.reference.scope
    snapshot = sources.capture_snapshot(
        CONTEXT, snapshot_id="expiry-backup-snapshot", scope=scope, conversation_id="conversation",
        runtime_version="source-expiry-v1", roots=(source.reference,),
    )
    backups, catalog = CountedBackupStore(), RecoveryCatalog()
    service = SourceBackupService(
        backups, SETTINGS, sources=sources, protection=PROTECTION, protection_verifier=protection_verifier(),
        recovery_authorizer=catalog, clock=clock,
    )
    service.initialize_protection()
    plan = service.plan(
        BACKUP_HUMAN, HUMAN_CONTEXT, backup_id=BACKUP_ID, scope=scope,
        snapshot_id="expiry-backup-snapshot", snapshot_receipt=snapshot,
    )
    reference, _ = uploaded(service, plan)
    clock.value = expiry
    with pytest.raises(Problem) as expired:
        sources.read_exact(CONTEXT, source.reference)
    assert code(expired) == "SOURCE_EXPIRED"
    with pytest.raises(Problem) as live_backup:
        service.plan(BACKUP_HUMAN, HUMAN_CONTEXT, backup_id="f" * 32, scope=scope,
                     snapshot_id="expiry-backup-snapshot", snapshot_receipt=snapshot)
    assert code(live_backup) == "SOURCE_EXPIRED"
    recovery = SourceBackupService(backups, SETTINGS, recovery_authorizer=catalog, clock=clock)
    with pytest.raises(Problem):
        recovery.restore_quarantined(BACKUP_HUMAN, reference)
    catalog.permit(reference)
    restored = recovery.restore_quarantined(BACKUP_HUMAN, reference)
    assert restored.status == "quarantined"
    assert restored.originals[source.reference.key] == original
    with pytest.raises(Problem) as not_reactivated:
        sources.read_exact(CONTEXT, source.reference)
    assert code(not_reactivated) == "SOURCE_EXPIRED"
