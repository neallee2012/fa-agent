"""A/B scoped registry pages and original empty-index receipts through real startup readers."""

import copy
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context.behavior_agent_registry import read_agent_registry
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_peer_runtime import _binding_agent
from shared_context.behavior_work_registry import WRITER_ENV, head_key, read_registry
from shared_context.behavior_work_registry import PAGE_SIZE, page_key
from shared_context.errors import Problem
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def register(c, control, identifier):
    command = InstructionSubmit(request_id=identifier, content="Synthetic original scoped registration.")
    return control.register(c.human, command.request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)


def test_two_scopes_preserve_empty_b_receipt_and_share_only_the_cost_ledger(peer_runtime, monkeypatch):
    c, a, b, policy = peer_runtime.c, peer_runtime.a, peer_runtime.b, peer_runtime.policy
    store = a.runtime.store
    ca, cb = a.runtime.work_policy, b.runtime.work_policy
    original_b = read_agent_registry(store, a.config, policy.agent_id)
    initial_receipt = store.reconcile(original_b.request)
    monkeypatch.setenv(WRITER_ENV, "false")
    old = register(c, ca, "a-original-legacy")
    before = store.read_snapshot((ca.registry_key, cb.registry_key))
    monkeypatch.setenv(WRITER_ENV, "true")
    first = register(c, ca, "same-rid")
    peer = register(c, cb, "same-rid")
    assert first.binding.binding_id != peer.binding.binding_id
    assert first.agent_scope is None and peer.agent_scope == policy.agent_scope
    assert store.read_snapshot((ca.registry_key, cb.registry_key)) == before
    assert read_agent_registry(store, a.config, policy.agent_id) == original_b
    assert store.reconcile(original_b.request) == initial_receipt
    assert read_registry(store, ca.policy).entries == (old.admission_id, first.admission_id)
    assert read_registry(store, cb.policy).entries == (peer.admission_id,)
    assert _binding_agent(a, peer.binding.binding_id) == policy.agent_id
    native = read_startup(c)
    assert sum(row.payload["data"].get("kind") == "steady_cumulative_cost_ledger" for row in native.rows.values()) == 1
    assert ca.cost_key == cb.cost_key
    monkeypatch.setenv(WRITER_ENV, "false")
    for control in (ca, cb):
        control.records.clear()
        control.refresh()
    assert set(ca.records) == {old.binding.binding_id, first.binding.binding_id}
    assert set(cb.records) == {peer.binding.binding_id}
    assert _binding_agent(a, peer.binding.binding_id) == policy.agent_id
    assert read_startup(c).entity_sha256 == native.entity_sha256


def test_page_admission_and_original_scope_snapshot_survive_cold_start(peer_runtime, monkeypatch):
    c, a, b = peer_runtime.c, peer_runtime.a, peer_runtime.b
    monkeypatch.setenv(WRITER_ENV, "true")
    monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "true")
    before = read_startup(c)
    for composition, identifier in ((a, "a-paged-submit"), (b, "b-paged-submit")):
        control = composition.runtime.work_policy
        binding, status = control.submit(c.human, InstructionSubmit(request_id=identifier, content="Original agent work."))
        assert status.state == "queued" and control.records[binding.binding_id].agent_scope == control.policy.agent_scope
    after = read_startup(c)
    assert before.rows[a.runtime.work_policy.cost_key] == after.rows[a.runtime.work_policy.cost_key]
    assert head_key(a.runtime.work_policy.registry_key) in after.rows
    assert head_key(b.runtime.work_policy.registry_key) in after.rows
    assert c.sessions.calls == []


def test_scoped_page_boundary_is_readable_and_partial_or_wrong_scope_history_is_refused(peer_runtime, monkeypatch):
    c, a, b = peer_runtime.c, peer_runtime.a, peer_runtime.b
    monkeypatch.setenv(WRITER_ENV, "true")
    cb = b.runtime.work_policy
    records = [register(c, cb, f"b-page-{number}") for number in range(PAGE_SIZE + 1)]
    inventory = read_startup(c)
    assert read_registry(b.runtime.store, cb.policy).entries == tuple(record.admission_id for record in records)
    assert read_registry(a.runtime.store, a.runtime.work_policy.policy).entries == ()
    key = page_key(cb.registry_key, 2)
    for damage in ("missing", "scope"):
        rows = dict(inventory.rows)
        if damage == "missing":
            rows.pop(key)
        else:
            altered = copy.deepcopy(rows[key].payload)
            altered["data"]["index_key"] = a.runtime.work_policy.registry_key
            rows[key] = replace(rows[key], payload=altered)
        snapshot = SimpleNamespace(read_snapshot=lambda keys: {name: rows.get(name) for name in keys})
        with pytest.raises(Problem):
            read_registry(snapshot, cb.policy)
    assert c.sessions.calls == []
