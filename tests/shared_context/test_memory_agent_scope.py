"""Memory metadata uses the saved peer scope, not another Agent's settings or runtime."""

from types import SimpleNamespace
from dataclasses import replace
import copy
import hashlib

import pytest

from shared_context.behavior_agent_registry import register_agent_scope
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_work_policy import (
    PerWorkRuntimePolicy, WorkPolicyControl, WorkRegistration, derive_work,
)
from shared_context.errors import Problem
from shared_context.memory_bridge_settings import (
    MemoryBridgeSettingsService, SelectMemoryBridgeSettings, apply_memory_capture, captured_policy_fields,
    capture_new_conversation_memory, memory_for_conversation, read_memory_settings,
    settings_head_key, settings_key,
)
from shared_context.source_materials import digest
from test_agent_scope_registry import peer_policy
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def memory_scopes(synthetic_standing_v1):
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    control = WorkPolicyControl(c.h.runtime, policy, c.control.cost_reader)
    # Only the metadata service seam is supplied here; B1c owns real peer execution/routing.
    runtime = SimpleNamespace(store=c.h.runtime.store, work_policy=control, _now=c.h.runtime._now,
                              authorize=c.h.runtime.authorize)
    return SimpleNamespace(
        case=c, a=MemoryBridgeSettingsService(c.h.runtime), b=MemoryBridgeSettingsService(runtime),
        b_control=control, b_runtime=runtime, b_policy=policy,
    )


def command(request_id="same-memory-setting", *, previous=None, enabled=True):
    return SelectMemoryBridgeSettings(
        schema_version="fa-memory-bridge-settings-command-v1",
        request_id=request_id, expected_settings_id=previous, enabled=enabled,
    )


def test_peer_settings_never_fall_back_or_overwrite_legacy_a(memory_scopes):
    x, c = memory_scopes, memory_scopes.case
    assert settings_key("authority", "same-memory-setting") == (
        "ca0904956c2bab62c2837b0bdcccaf4783eccf71b725293c0a9281e1bfb0c003")
    assert settings_head_key("authority") == "18077958c2d5641fb9c44a6fbf1816d1695ca3290c292bd564352daac4c7828b"
    first = x.a.select(c.human, command())
    aid = first.payload["result"]["settings_id"]
    akeys = (aid, settings_head_key(c.control.policy.authority_domain), c.control.cost_key)
    before = c.h.runtime.store.read_snapshot(akeys)
    assert "agent_id" not in before[aid].payload["data"]
    assert set(before[akeys[1]].payload["data"]) == {"kind", "settings_id"}

    empty_b = x.b.current(c.human)
    assert empty_b["settings_id"] is None and empty_b["enabled"] is False
    second = x.b.select(c.human, command(enabled=False))
    bid = second.payload["result"]["settings_id"]
    assert bid != aid
    assert x.b.current(c.human)["settings_id"] == bid
    assert x.a.current(c.human)["settings_id"] == aid
    assert x.a.current(c.human)["enabled"] is True
    assert x.b.current(c.human)["enabled"] is False
    assert x.a.reconcile(c.human, command()) == first
    assert x.b.reconcile(c.human, command(enabled=False)) == second
    assert c.h.runtime.store.read_snapshot(akeys) == before
    assert c.control.cost_key == x.b_control.cost_key
    assert c.sessions.calls == []


def test_a_and_b_settings_remain_readable_together_at_startup(memory_scopes):
    from shared_context.memory_bridge_settings import validate_memory_settings_history

    x, c = memory_scopes, memory_scopes.case
    a = x.a.select(c.human, command()).payload["result"]["settings_id"]
    b = x.b.select(c.human, command(enabled=False)).payload["result"]["settings_id"]
    inventory = read_startup(c)
    assert set(validate_memory_settings_history(inventory, c.h.config, c.control.policy)) == {a}
    assert set(validate_memory_settings_history(inventory, c.h.config, x.b_policy)) == {b}
    assert sum(row.payload["data"].get("kind") == "steady_cumulative_cost_ledger"
               for row in inventory.rows.values()) == 1


def test_new_b_capture_uses_only_its_own_head_and_rejects_a_setting_ids(memory_scopes):
    x, c = memory_scopes, memory_scopes.case
    aid = x.a.select(c.human, command()).payload["result"]["settings_id"]
    empty = capture_new_conversation_memory(x.b_control, c.human)
    assert empty.value is None
    assert set(empty.predicates) == {
        settings_head_key(x.b_policy.authority_domain, x.b_policy.agent_scope)}
    bid = x.b.select(c.human, command()).payload["result"]["settings_id"]
    captured = capture_new_conversation_memory(x.b_control, c.human)
    assert captured.value.settings_id == bid and captured.value.enabled
    assert aid not in captured.predicates
    for policy, foreign in ((c.control.policy, bid), (x.b_policy, aid)):
        with pytest.raises(Problem) as denied:
            read_memory_settings(c.h.runtime.store, policy, foreign)
        assert denied.value.body["error"]["code"] == "MEMORY_SETTINGS_INVALID"


def cached_registration(policy, capture, now):
    """Typed cold-reader input only; do not fabricate a peer runtime or persisted admission."""
    submitted = InstructionSubmit(request_id="captured-b-turn", content="Keep this original B scope.")
    admission, binding, profile = derive_work(policy, submitted.request_id, "native", instruction=submitted)
    if capture is not None:
        profile, binding = apply_memory_capture(profile, binding, admission, capture, selected_template=None)
    runtime_policy = PerWorkRuntimePolicy(**{
        **policy.legacy_template_policy.model_dump(), **captured_policy_fields(capture),
        "binding_digest": digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")]),
        "policy_id": policy.policy_id, "policy_revision": policy.revision, "admission_id": admission,
        "job_id": binding.job_id, "stage": "queued", "accepted_at": now, "queue_deadline": None,
        "valid_until": None, "instruction": True,
    })
    return WorkRegistration(
        admission_id=admission, request_id=submitted.request_id,
        submit_sha256=hashlib.sha256(submitted.model_dump_json().encode()).hexdigest(),
        policy_id=policy.policy_id, policy_revision=policy.revision, policy_sha256=policy.sha256,
        principal_oid=binding.human_oid, binding=binding, profile=profile, runtime_policy=runtime_policy,
        agent_scope=policy.agent_scope,
    )


def test_existing_b_keeps_saved_capture_not_new_head_or_environment(memory_scopes, monkeypatch):
    x, c = memory_scopes, memory_scopes.case
    first = x.b.select(c.human, command()).payload["result"]["settings_id"]
    captured = capture_new_conversation_memory(x.b_control, c.human).value
    original = cached_registration(x.b_policy, captured, c.h.time)
    x.b_control.records[original.binding.binding_id] = original
    x.b.select(c.human, command("b-now-disabled", previous=first, enabled=False))
    x.a.select(c.human, command())
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", "not-a-profile")
    monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSION", "not-a-version")
    conversation = SimpleNamespace(jobs=(original.binding.job_id,))
    observed = memory_for_conversation(x.b_control, c.human, conversation)
    assert observed.value == captured and observed.value.settings_id == first
    assert set(observed.predicates) == {first}
    assert capture_new_conversation_memory(x.b_control, c.human).value.enabled is False


def test_existing_conversation_cannot_use_a_registration_from_another_scope(memory_scopes):
    x, c = memory_scopes, memory_scopes.case
    original_a = cached_registration(c.control.policy, None, c.h.time)
    x.b_control.records[original_a.binding.binding_id] = original_a
    with pytest.raises(Problem) as denied:
        memory_for_conversation(x.b_control, c.human, SimpleNamespace(jobs=(original_a.binding.job_id,)))
    assert denied.value.body["error"]["code"] == "MEMORY_SETTINGS_INVALID"


def test_b_unknown_setting_only_reads_its_original_receipt(memory_scopes, monkeypatch):
    x, c = memory_scopes, memory_scopes.case
    aid = x.a.select(c.human, command()).payload["result"]["settings_id"]
    request_id = settings_key(x.b_policy.authority_domain, "same-memory-setting", x.b_policy.agent_scope)
    original_submit, attempts = c.h.runtime.store._submit, []

    def lose_response(operations, request):
        if request is not None and request.request_id == request_id:
            attempts.append(request.request_id)
            c.h.table.after_apply = "lost"
        try:
            return original_submit(operations, request)
        finally:
            c.h.table.after_apply = None

    monkeypatch.setattr(c.h.runtime.store, "_submit", lose_response)
    with pytest.raises(Problem) as unknown:
        x.b.select(c.human, command())
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    before = c.h.runtime.store._guard()
    monkeypatch.setattr(c.h.runtime.store, "transact", lambda *_: pytest.fail("reconcile must not write"))
    receipt = x.b.reconcile(c.human, command())
    assert receipt.payload["result"]["settings_id"] == request_id != aid
    assert attempts == [request_id] and c.h.runtime.store._guard() == before
    assert x.a.current(c.human)["settings_id"] == aid


def test_b_head_with_an_a_pointer_is_not_ignored_by_startup(memory_scopes):
    from shared_context.memory_bridge_settings import validate_memory_settings_history

    x, c = memory_scopes, memory_scopes.case
    aid = x.a.select(c.human, command()).payload["result"]["settings_id"]
    x.b.select(c.human, command())
    inventory = read_startup(c)
    bhead = settings_head_key(x.b_policy.authority_domain, x.b_policy.agent_scope)
    original = inventory.rows[bhead]
    payload = copy.deepcopy(original.payload)
    payload["data"]["settings_id"] = aid
    bad = replace(inventory, rows={**inventory.rows, bhead: replace(original, payload=payload)})
    with pytest.raises(Problem):
        validate_memory_settings_history(bad, c.h.config, c.control.policy)


def test_b_revocation_blocks_memory_without_revoking_a(memory_scopes):
    x, c = memory_scopes, memory_scopes.case
    aid = x.a.select(c.human, command()).payload["result"]["settings_id"]
    x.b.select(c.human, command())
    x.b_control.revoke(c.human, "revoke-b-memory")
    before = c.h.runtime.store._guard()
    for read in (lambda: x.b.current(c.human),
                 lambda: x.b.reconcile(c.human, command()),
                 lambda: capture_new_conversation_memory(x.b_control, c.human)):
        with pytest.raises(Problem) as denied:
            read()
        assert denied.value.body["error"]["code"] == "WORK_POLICY_REVOKED"
    assert x.a.current(c.human)["settings_id"] == aid
    assert c.h.runtime.store._guard() == before


def test_b_does_not_recall_a_note_even_with_the_same_team_human_and_subject(memory_scopes):
    from shared_context.behavior_reviewed_note_source import resolve_reviewed_note

    x, c = memory_scopes, memory_scopes.case
    original = saved_turn(c, "a-note-owner-scope").binding
    assert c.h.runtime.authorize(original.binding_id, c.human, "read")[0] == original
    destination = x.b_policy.template.model_copy(update={"subject": original.subject})
    assert (destination.tenant_id, destination.team_id, destination.human_oid) == (
        original.tenant_id, original.team_id, original.human_oid)
    assert destination.agent_oid != original.agent_oid
    with pytest.raises(Problem) as denied:
        resolve_reviewed_note(c.h.runtime, c.human, original.binding_id,
                              c.control.policy.profile.claim_id, destination,
                              c.control.policy.legacy_template_policy.purpose)
    assert denied.value.body["error"]["code"] == "MEMORY_RECALL_SCOPE_DENIED"
