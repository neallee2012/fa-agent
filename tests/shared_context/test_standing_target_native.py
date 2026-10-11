"""One necessary target/history/new-grant integration; no real Hosted/SDK/provider."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.behavior_foundry import native_key
from shared_context.behavior_migration import code_sha256
from shared_context.behavior_work_policy import work_policy_key
from shared_context.behavior_work_target import TargetSelection, select_target, activate_target
from shared_context.errors import Problem
from test_work_policy_native import (
    retained_handoff, installer, current_machine_evidence, stopped_original,
    original_teams_gap, original_finite_worker_contract, next_window, next_review, original_version,
    bounded_initial_control_quota, closed_v2, original_completed_nonempty_page, installed_empty, operator,
    exact_source_initialization, fixed_original_high_water, expired_claim, source_contract, closed_case,
    offline_socket_guard, original_captured_route_producer, closed_finished, sufficient_initial_budget,
    durable_result, native_acquire, current_rotation, two_rotations, current_cancelled, rotated,
    jwk_client, signing_key,
)

pytestmark = pytest.mark.parametrize("source_contract", ["claim"], indirect=True)


def test_one_successor_target_preserves_standing_history_and_new_work_uses_it(stopped_original, tmp_path, monkeypatch):
    import subprocess
    from copilot import CopilotClient
    def denied(*args, **kwargs):
        pytest.fail("new target integration cannot execute runtime/provider")
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(CopilotClient, "start", denied)
    c, _, _, _ = stopped_original
    handoff = retained_handoff(stopped_original)
    install, costs = installer(c, handoff, tmp_path)
    plan = install.plan(c.principal, installation_id="offline-target-standing")
    approval = install.authorize(c.principal, plan, decision_id="offline-target",
                                 expires_at=c.clock[0] + timedelta(minutes=4))
    install.apply(c.principal, plan, approval)
    install.source(c.principal, plan, approval)
    c.parent.installation()
    current_machine_evidence(c, install)
    native, workers = c.runtime.dep.native_launches, c.runtime.dep.workers
    # The existing fixture replaces the start transport; restore its actual
    # configured executor for activate_target, without invoking that executor.
    from shared_context.behavior_foundry import FoundryExecutor
    clients = tuple(client for client in c.parent.clients if hasattr(client, "target") and hasattr(client, "start"))
    executor = FoundryExecutor(native, clients)
    workers.adapter = executor.adapter(verify_readiness=c.parent.catalog.worker)
    old, = c.config.active_targets
    successor = old.model_copy(update={"agent_version": str(int(old.agent_version) + 1),
                                      "session_id": "offline-successor-session"})
    command = TargetSelection(configuration_sha256=c.config.sha256, code_sha256=code_sha256(),
        previous=old, target=successor, image_sha256="c" * 64,
        credential_client_id=c.config.identity.managed_identity_client_id,
        session_owner_oid="88888888-8888-4888-8888-888888888888",
        approval_reference="offline-new-hosted-target")
    before = c.store.read_inventory()
    old_generation = before.rows[native_key("target", old.executor_id)].payload["data"]["generation"]
    selected = select_target(c.parent, command, c.principal)
    c.parent.installation()
    assert native.targets == (successor,) and executor.clients[old.executor_id].target == successor
    after = c.store.read_inventory()
    targetkey = native_key("target", old.executor_id)
    for key, row in before.rows.items():
        if key != targetkey:
            assert after.rows[key] == row
    assert all(after.receipts[key] == receipt for key, receipt in before.receipts.items())
    frozen = c.store.read_inventory()
    assert select_target(c.parent, command, c.principal, reconcile=True) == selected
    assert c.store.read_inventory() == frozen
    b, status = c.runtime.work_policy.submit(c.principal, AssistantSubmit(request_id="offline-new-target-work"))
    assert status.state == "queued"
    grantkey = native_key("grant", native_key("bootstrap-grant", b.job_id))
    assert c.store.read_snapshot((grantkey,))[grantkey].payload["data"]["targets"] == [successor.model_dump(mode="json")]
    c.parent.installation()
    captured = []
    workers.adapter = replace(workers.adapter, start=lambda start: captured.append(native.prepare(start)))
    workers.start(b.job_id, b.owner)
    launch, _ = captured[0]
    assert launch.target == successor and launch.process_generation == old_generation + 1
    c.parent.installation()
    assert c.config.active_targets == (old,) and c.config.sha256 == command.configuration_sha256
    print("PASS: target selection -> actual standing history -> new one-use grant/start prepared for new version/SID; no provider call")
