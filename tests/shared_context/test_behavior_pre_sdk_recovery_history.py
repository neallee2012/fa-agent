import copy
from unittest.mock import patch

import pytest

from shared_context.behavior_assistant import _ModelIntent, key as assistant_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_recovery_history import validate_recovery_history
from shared_context.errors import Problem
from shared_context.records import Record
from test_behavior_recovery_history import history, inventory_copy, change_row, forbidden


pytestmark = pytest.mark.parametrize("history", ["pre-sdk"], indirect=True)


def test_pre_sdk_history_preserves_actual_cancel_receipts_without_model_liability(history):
    h = history
    original = copy.deepcopy(h.table.backing.rows)
    with patch.object(h.store, "read_snapshot", forbidden), patch.object(h.store, "transact", forbidden):
        classified = validate_recovery_history(h.inventory, h.config)
    assert len(classified) == 5
    assert h.table.backing.rows == original
    assert h.approval.plan.model_request_sha256 is None
    assert h.approval.plan.recovery_phase == "before_sdk_acquire"
    b = h.config.bindings[0]
    result = h.inventory.rows[control_key("delivery-work-state", b.obligation_id)].payload["data"]["result"]
    assert result["state"] == "cancelled" and result["unresolved_operations"] == []
    assert h.inventory.rows[control_key("budget", b.owner.root_budget_id)] == h.before_cancel.rows[
        control_key("budget", b.owner.root_budget_id)]
    assert h.inventory.rows[control_key("delivery-obligation", b.obligation_id)].payload["data"]["fulfillment"] == "pending"
    assert all(h.inventory.receipts[key] == receipt for key, receipt in h.before_cancel.receipts.items())


@pytest.mark.parametrize("mutation", ["session-running", "acquire-receipt", "model-intent"])
def test_pre_sdk_history_cannot_relabel_an_acquired_or_model_sending_attempt(history, mutation):
    h = history
    inventory = inventory_copy(h)
    sid = assistant_key("session-id", h.approval.plan.binding.start_request_id)
    if mutation == "session-running":
        change_row(inventory, assistant_key("session", sid), lambda value: value["data"].update(state="running"))
    elif mutation == "acquire-receipt":
        inventory.receipts[assistant_key("acquire", sid, "sdk-start")] = next(iter(inventory.receipts.values()))
    else:
        model = _ModelIntent(request_sha256="e" * 64, consumed=False)
        key = assistant_key("model-intent", sid, model.request_sha256)
        sample = inventory.rows[assistant_key("session", sid)]
        inventory.rows[key] = Record(
            {**sample.payload, "row_id": key, "data": model.model_dump(mode="json"), "material": None}, sample.etag,
        )
    with pytest.raises(Problem):
        validate_recovery_history(inventory, h.config)
