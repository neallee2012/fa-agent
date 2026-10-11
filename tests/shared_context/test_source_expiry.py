"""Expiry contracts over source interfaces; database transport is offline."""

import hashlib
from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from shared_context.behavior_store import BehaviorStore
from shared_context.errors import Problem
from shared_context.source_materials import SourceMaterials, SourcePlan, canonical
from test_behavior_store import FakeTable, SCOPE
from test_source_materials import CONTEXT, NOW, code, input_source


class Clock:
    def __init__(self):
        self.value = NOW

    def __call__(self):
        return self.value


def expiring_service():
    clock = Clock()
    table = FakeTable()
    store = BehaviorStore(table, SCOPE)
    store.initialize()
    return SourceMaterials(store, clock=clock), table, clock


def test_exact_source_expires_at_server_deadline_but_original_receipt_still_reconciles():
    sources, _, clock = expiring_service()
    expiry = NOW + timedelta(seconds=1)
    plan = sources.plan(CONTEXT, input_source(expires_at=expiry), b"retained exact original")
    receipt = sources.commit(CONTEXT, plan)
    assert sources.read_exact(CONTEXT, plan.reference).original == b"retained exact original"
    clock.value = expiry
    with pytest.raises(Problem) as expired:
        sources.read_exact(CONTEXT, plan.reference)
    assert code(expired) == "SOURCE_EXPIRED"
    assert sources.reconcile(CONTEXT, plan) == receipt


@pytest.mark.parametrize("expiry", [NOW, NOW - timedelta(microseconds=1)])
def test_source_admission_requires_expiry_later_than_trusted_server_clock(expiry):
    sources, _, _ = expiring_service()
    with pytest.raises(Problem) as invalid:
        sources.plan(CONTEXT, input_source(expires_at=expiry), b"already expired")
    assert code(invalid) == "SOURCE_EXPIRED"


@pytest.mark.parametrize("expiry", [
    NOW.replace(tzinfo=None),
    NOW.astimezone(timezone(timedelta(hours=8))),
])
def test_source_expiry_requires_explicit_UTC(expiry):
    with pytest.raises(ValidationError):
        input_source(expires_at=expiry)


def test_source_prepared_before_expiry_cannot_commit_after_expiry():
    sources, _, clock = expiring_service()
    expiry = NOW + timedelta(seconds=1)
    plan = sources.plan(CONTEXT, input_source(expires_at=expiry), b"not yet admitted")
    clock.value = expiry
    with pytest.raises(Problem) as expired:
        sources.commit(CONTEXT, plan)
    assert code(expired) == "SOURCE_EXPIRED"
    with pytest.raises(Problem):
        sources.reconcile(CONTEXT, plan)


def test_absent_and_null_expiry_keep_legacy_plan_event_and_reconcile_digests():
    sources, _, clock = expiring_service()
    source = input_source(source_id="expiry-legacy")
    explicit_none = input_source(source_id="expiry-legacy", expires_at=None)
    assert explicit_none.model_dump(mode="json") == source.model_dump(mode="json")
    assert "expires_at" not in explicit_none.model_dump()
    plan = sources.plan(CONTEXT, source, b"legacy-original-v1")
    assert hashlib.sha256(plan.model_dump_json().encode()).hexdigest() == (
        "04d9a329002204f4d22569ff7d3b7ec4ae1a22d60fdfc93d509dc74be155aff4"
    )
    assert plan.request.request_digest == "bb739a686976d00abf38cf3b5225e2b21a4de1af5a43145d15e1ffd4e1ada498"
    retained = SourcePlan.model_validate_json(plan.model_dump_json())
    receipt = sources.commit(CONTEXT, retained)
    event = sources.read_exact(CONTEXT, retained.reference).event
    assert "expires_at" not in event.model_dump()
    assert hashlib.sha256(canonical(event.model_dump(mode="json"))).hexdigest() == (
        "c87962d474b1d594e74fff6d7994ac136b5050fa534ae0638f62279778888cbc"
    )
    clock.value += timedelta(days=3650)
    assert sources.plan(CONTEXT, explicit_none, b"legacy-original-v1") == retained
    assert sources.commit(CONTEXT, retained) == receipt
    assert sources.reconcile(CONTEXT, retained) == receipt
    assert sources.read_exact(CONTEXT, retained.reference).original == b"legacy-original-v1"


@pytest.mark.parametrize("replacement_expiry", [None, NOW + timedelta(hours=1)])
def test_same_version_cannot_remove_or_extend_its_original_deadline(replacement_expiry):
    sources, _, clock = expiring_service()
    expiry = NOW + timedelta(seconds=1)
    plan = sources.plan(CONTEXT, input_source(expires_at=expiry), b"immutable applicability")
    receipt = sources.commit(CONTEXT, plan)
    retained = SourcePlan.model_validate_json(plan.model_dump_json())
    assert retained.source.expires_at == expiry
    changed = sources.plan(CONTEXT, input_source(expires_at=replacement_expiry), plan.original)
    with pytest.raises(Problem) as conflict:
        sources.commit(CONTEXT, changed)
    assert code(conflict) == "BEHAVIOR_REQUEST_CONFLICT"
    assert sources.read_exact(CONTEXT, plan.reference).event.expires_at == expiry
    clock.value = expiry
    with pytest.raises(Problem) as expired:
        sources.read_exact(CONTEXT, plan.reference)
    assert code(expired) == "SOURCE_EXPIRED"
    assert sources.reconcile(CONTEXT, retained) == receipt


def test_correction_is_a_new_version_not_an_extension_of_expired_history():
    sources, _, clock = expiring_service()
    expiry = NOW + timedelta(seconds=1)
    first = sources.plan(CONTEXT, input_source(expires_at=expiry), b"first exact bytes")
    sources.commit(CONTEXT, first)
    clock.value = expiry
    second = sources.plan(CONTEXT, input_source(version=2, expires_at=expiry + timedelta(hours=1)), b"corrected bytes")
    sources.commit(CONTEXT, second)
    assert sources.read_exact(CONTEXT, second.reference, require_current=True).original == b"corrected bytes"
    with pytest.raises(Problem) as old:
        sources.read_exact(CONTEXT, first.reference)
    assert code(old) == "SOURCE_EXPIRED"


def test_expired_necessary_source_denies_attachment_and_live_snapshot_restore():
    sources, _, clock = expiring_service()
    expiry = NOW + timedelta(seconds=1)
    leaf = sources.plan(CONTEXT, input_source(source_id="leaf", expires_at=expiry), b"necessary leaf")
    sources.commit(CONTEXT, leaf)
    root = sources.plan(CONTEXT, input_source(source_id="root", necessary=(leaf.reference,)), b"unexpired root")
    sources.validate_attachment(CONTEXT, root)
    rows = sources.read_snapshot(root.request.read_set)
    snapshot = sources.plan_snapshot(
        CONTEXT, snapshot_id="expiry-snapshot", scope=leaf.reference.scope,
        conversation_id="expiry-conversation", runtime_version="expiry-contract", roots=(leaf.reference,),
    )
    receipt = sources.commit_snapshot(CONTEXT, snapshot)
    clock.value = expiry
    for operation in (
        lambda: sources.attachment(CONTEXT, root, rows),
        lambda: sources.commit(CONTEXT, root),
        lambda: sources.restore_snapshot(CONTEXT, scope=leaf.reference.scope,
                                          snapshot_id="expiry-snapshot", receipt=receipt),
    ):
        with pytest.raises(Problem) as expired:
            operation()
        assert code(expired) == "SOURCE_EXPIRED"
    assert sources.reconcile_snapshot(CONTEXT, snapshot) == receipt
