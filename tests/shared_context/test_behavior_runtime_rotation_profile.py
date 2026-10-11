"""Offline same-contract SDK profile rotation; no parent input files are changed."""
import json

import pytest

from shared_context import behavior_runtime_rotation as rotation
from shared_context.behavior_foundry import native_key
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime_rotation_history import binding_scopes
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.errors import Problem
from shared_context.memory_governance import SubjectState
from shared_context.source_materials import digest
from test_behavior_runtime_rotation import approved, rotated, write


def profile(h, *, uses=2, fresh_subject=True):
    raw = h.new.model_dump(mode="json")
    if fresh_subject:
        raw["bindings"][0]["subject"] = "successor-subject-r2"
    raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", raw["bindings"][0]])
    new = ServerConfiguration.model_validate_json(json.dumps(raw))
    retirement = json.loads(h.paths.retirement.read_bytes())
    retirement["new_configuration_sha256"] = new.sha256
    h.paths.retirement.write_text(json.dumps(retirement), encoding="utf-8")
    grants = tuple(grant.model_copy(update={"remaining_uses": uses}) for grant in h.service.grants)
    h.service = rotation.RuntimeRotation(
        h.store, h.old, new, h.paths, native_grants=grants, clock=h.service.clock, allow_apply=True,
    )
    h.new = new


@pytest.mark.parametrize("uses", [1, 2, 4])
def test_same_worker_contract_accepts_fresh_bounded_multi_use_grant_without_refill(rotated, uses):
    h = rotated
    profile(h, uses=uses)
    assert h.old.workers == h.new.workers
    assert h.old.delivery == h.new.delivery
    before = h.store.read_inventory()
    plan, auth = approved(h)
    assert plan.subject_lineage.previous_subject == h.old.bindings[0].subject
    assert plan.subject_lineage.successor_subject == h.new.bindings[0].subject
    h.service.apply(h.principal, plan, auth)
    result = h.service.verify(h.principal, plan, auth)
    after = h.store.read_inventory()
    grant = h.service.grants[0]
    assert after.rows[native_key("grant", grant.grant_id)].payload["data"]["remaining_uses"] == uses
    assert result["liability"]["model_outcome"] == "unknown"
    assert result["subject_lineage"] == plan.subject_lineage.model_dump(mode="json")
    assert all(after.receipts[key] == value for key, value in before.receipts.items())
    for key, value in before.rows.items():
        if value.payload["data"].get("kind") in {"native_execution_grant", "operation_budget"}:
            assert after.rows[key] == value


@pytest.mark.parametrize("uses", [0, 5])
def test_grant_use_bound_is_still_enforced_before_apply(rotated, uses):
    h = rotated
    profile(h, uses=uses)
    count = len(h.table.submissions)
    with pytest.raises(Problem):
        h.service.plan(h.principal, rotation_id="outside-grant-bound")
    assert len(h.table.submissions) == count


def test_new_subject_is_linked_and_old_index_is_protected_not_hidden_or_rewritten(rotated):
    h = rotated
    scope = binding_scopes(h.old, h.old.bindings[0])[0]
    old_key = control_key("memory-subject", scope.key, h.old.bindings[0].subject)
    state = SubjectState(scope=scope, subject=h.old.bindings[0].subject, sequence=9)
    write(h.store, old_key, state.model_dump(mode="json"))
    old_record = h.store.read_snapshot((old_key,))[old_key]
    profile(h)
    plan, auth = approved(h)
    h.service.apply(h.principal, plan, auth)
    h.service.verify(h.principal, plan, auth)
    current = MigrationStore(h.table, h.store._scope)
    assert installed_origin(current, h.new) == h.original
    assert current.read_snapshot((old_key,))[old_key] == old_record
    with pytest.raises(Problem):
        write(current, old_key, state.model_copy(update={"sequence": 10}).model_dump(mode="json"))
    new_key = control_key("memory-subject", scope.key, h.new.bindings[0].subject)
    successor = SubjectState(scope=scope, subject=h.new.bindings[0].subject, sequence=1)
    write(current, new_key, successor.model_dump(mode="json"))
    assert installed_origin(MigrationStore(h.table, h.store._scope), h.new) == h.original
    assert current.read_snapshot((old_key,))[old_key] == old_record


@pytest.mark.parametrize("change", ["source-namespace", "source-version", "old-native-grant"])
def test_fresh_subject_never_waives_source_scope_or_old_grant_fences(rotated, change):
    h = rotated
    if change == "old-native-grant":
        profile(h)
        h.service.grants = (h.service.grants[0].model_copy(update={"grant_id": "original-native-grant"}),)
        with pytest.raises(Problem):
            h.service.plan(h.principal, rotation_id="reused-old-grant")
        return
    raw = h.new.model_dump(mode="json")
    raw["bindings"][0]["subject"] = "fresh-but-unrelated"
    if change == "source-namespace":
        raw["policies"][0]["policy"]["source_visibility"] = "private"
    else:
        raw["assistant"]["profiles"][0]["source"]["version"] += 1
    raw["policies"][0]["policy"]["binding_digest"] = digest(["fa-behavior-api-v1", "native", raw["bindings"][0]])
    candidate = ServerConfiguration.model_validate_json(json.dumps(raw))
    with pytest.raises(Problem):
        rotation._configuration_change(h.old, candidate)


def test_subject_lineage_is_part_of_the_human_approval_hash(rotated):
    h = rotated
    profile(h)
    plan, auth = approved(h)
    other = plan.model_copy(update={"subject_lineage": plan.subject_lineage.model_copy(
        update={"previous_subject": "hidden-predecessor"})})
    assert other.sha256 != plan.sha256
    count = len(h.table.submissions)
    with pytest.raises(Problem):
        h.service.apply(h.principal, other, auth)
    assert len(h.table.submissions) == count
