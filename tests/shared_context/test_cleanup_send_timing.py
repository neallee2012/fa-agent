"""Original cleanup producer with elapsed-time validation, not a fixed clock."""
import copy
from datetime import timedelta
import json
from time import monotonic

import pytest

pytest.importorskip("test_channel_window", reason="pinned offline original Window2 fixture required")
from test_teams_expired_cleanup import (
    original_teams_gap, original_finite_worker_contract, next_window, next_review, original_version,
    bounded_initial_control_quota, closed_v2, original_completed_nonempty_page, installed_empty, operator,
    exact_source_initialization, fixed_original_high_water, expired_claim, source_contract, closed_case,
    offline_socket_guard, original_captured_route_producer, closed_finished, sufficient_initial_budget,
    durable_result, native_acquire, current_rotation, two_rotations, current_cancelled, rotated,
    jwk_client, signing_key, cleanup_service,
)
from shared_context.behavior_operations import control_key
from shared_context.teams_delivery import TeamsDeliveryAdapter
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.errors import Problem

pytestmark = pytest.mark.parametrize("source_contract", ["claim"], indirect=True)


def test_full_cleanup_validation_finishes_before_two_second_single_send(original_teams_gap, monkeypatch):
    c = original_teams_gap
    c.clock[0] = c.config.channels.valid_until + timedelta(minutes=1)
    service, stops = cleanup_service(c)
    authorization = service.authorize(c.principal, service.plan(c.principal), "offline-advancing-clock")
    assert service.execute(c.principal, authorization)["state"] == "cancelled"
    baseline, sequence, instant = copy.deepcopy(c.transport.backing.rows), c.transport.backing.sequence, c.clock[0]
    original_consume, write_gate = TeamsDeliveryAdapter._consume, service._write_gate
    did = control_key("delivery-final-id", c.work.binding.obligation_id)
    in_consume, validations = [], []
    def consume(*args):
        in_consume.append(True)
        try:
            return original_consume(*args)
        finally:
            in_consume.pop()
    monkeypatch.setattr(TeamsDeliveryAdapter, "_consume", consume)
    results = []
    for delay in (0, 3, 13.273294):
        c.transport.backing.rows, c.transport.backing.sequence = copy.deepcopy(baseline), sequence
        c.clock[0], c.sends[:] = instant, []
        validations.clear()
        def validate(*args):
            write_gate(*args)
            if in_consume:
                c.clock[0] += timedelta(seconds=delay)
                validations.append(c.clock[0])
        monkeypatch.setattr(service, "_write_gate", validate)
        begin = monotonic()
        receipt = service.deliver(c.principal, authorization)
        send = c.parent.teams.channel.adapter._send_row(did)
        result = {
            "injected_full_check_seconds": delay,
            "actual_elapsed_seconds": round(monotonic()-begin, 3),
            "validation_completed_at": validations[-1].isoformat(),
            "gated_at": send.gated_at.isoformat(), "not_after": send.not_after.isoformat(),
            "current_at": c.clock[0].isoformat(), "connector_posts": len(c.sends),
            "fulfillment": receipt.payload["result"]["fulfillment"],
        }
        results.append(result)
        print(json.dumps(result))
        assert send.not_after-send.gated_at == timedelta(seconds=2)
        assert c.sends == ["original-Teams-result"] and receipt.payload["result"]["fulfillment"] == "fulfilled"
        assert send.gated_at >= validations[-1]
        installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
    assert len(stops) == 1 and c.workers.inspect(c.work.binding.job_id).queries_reserved == 9


def test_expiry_after_timed_decision_does_not_commit_consumption_or_post(original_teams_gap, monkeypatch):
    c = original_teams_gap
    c.clock[0] = c.config.channels.valid_until + timedelta(minutes=1)
    service, stops = cleanup_service(c)
    authorization = service.authorize(c.principal, service.plan(c.principal), "offline-original-short-deadline")
    service.execute(c.principal, authorization)
    original_guard = c.store._guard
    advanced = []
    def guard():
        import sys
        result = original_guard()
        if sys._getframe(1).f_code.co_name == "final" and not advanced:
            c.clock[0] += timedelta(seconds=3)
            advanced.append(True)
        return result
    monkeypatch.setattr(c.store, "_guard", guard)
    with pytest.raises(Problem) as expired:
        service.deliver(c.principal, authorization)
    assert expired.value.body["error"]["code"] == "TEAMS_CLEANUP_DELIVERY_EXPIRED"
    assert advanced == [True] and c.sends == [] and len(stops) == 1
    did = control_key("delivery-final-id", c.work.binding.obligation_id)
    send = c.parent.teams.channel.adapter._send_row(did)
    assert send.phase == "claimed" and send.gate_request is None
    receipt_id = control_key("teams-consume", did)
    assert receipt_id not in c.store.read_inventory().receipts
    installed_origin(MigrationStore(c.parent.clients[0], c.config.table.scope), c.config)
    with pytest.raises(Problem):
        service.deliver(c.principal, authorization)
    assert c.sends == []
