from dataclasses import replace

import pytest

from shared_context.behavior_recovery import recovery_key
from shared_context.behavior_migration import installation_record, protection_keys
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime_rotation_history import validate_additions
from shared_context.errors import Problem
from test_behavior_recovery_history import history, inventory_copy, change_row


def retained_baseline(h):
    return {
        installation_record(h.config)[0], *protection_keys(h.config),
        *(control_key("server-authority-probe", h.config.sha256, kind) for kind in ("budget", "winner", "proof")),
    }


@pytest.mark.parametrize("history", ["pre-sdk", 204], indirect=True)
def test_rotated_current_runtime_classifies_actual_completed_recovery_producers(history):
    h = history
    validate_additions(h.inventory, h.config, h.config, retained_baseline(h), set())


@pytest.mark.parametrize("history", ["pre-sdk"], indirect=True)
def test_rotated_current_recovery_rejects_missing_original_stop_receipt(history):
    h = history
    broken = inventory_copy(h)
    stop_key = recovery_key("stop-observation", h.approval.plan.recovery_id)
    del broken.receipts[stop_key]
    with pytest.raises(Problem):
        validate_additions(broken, h.config, h.config, retained_baseline(h), set())


@pytest.mark.parametrize("history", ["pre-sdk"], indirect=True)
def test_rotated_current_recovery_never_accepts_relabelled_prior_configuration(history):
    h = history
    broken = inventory_copy(h)
    authorization = recovery_key("authorization", h.approval.plan.recovery_id)
    change_row(broken, authorization, lambda row: row["data"]["plan"].update(configuration_sha256="a" * 64))
    with pytest.raises(Problem):
        validate_additions(broken, h.config, h.config, retained_baseline(h), set())


@pytest.mark.parametrize("history", ["pre-sdk"], indirect=True)
def test_rotated_current_recovery_does_not_drop_unrecognized_new_rows(history):
    h = history
    broken = inventory_copy(h)
    item = next(iter(broken.rows.values()))
    changed = {**item.payload, "row_id": "unrecognized-current-row", "data": {"not_a_recovery": True}}
    broken.rows["unrecognized-current-row"] = replace(item, payload=changed)
    with pytest.raises(Problem):
        validate_additions(broken, h.config, h.config, retained_baseline(h), set())
