"""Ordinary validated writes keep the original hook order."""
from shared_context.behavior_store import ControlChanges
from test_behavior_migration_store import store_with_row
from test_behavior_store import request


def test_ordinary_hook_runs_after_decision_then_inherited_check():
    store, _ = store_with_row()
    order = []
    def decision(_):
        order.append("normal-decision")
        return ControlChanges(result={})
    store.validated_writes(lambda: store.transact(request("normal"), decision,
        before_send=lambda: order.append("normal-before-send")), lambda: order.append("normal-inherited"))
    assert order == ["normal-decision", "normal-before-send", "normal-inherited"]
