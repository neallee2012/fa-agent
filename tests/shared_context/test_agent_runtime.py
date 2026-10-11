"""C1 peer runtimes share the authority/store, not the other Agent's work or target."""

import pytest
import json
import hashlib
from dataclasses import replace
from types import SimpleNamespace

from shared_context.behavior_agent_registry import register_agent_scope
from shared_context.errors import Problem
from shared_context.behavior_assistant_wire import InstructionSubmit
from test_agent_scope_registry import peer_policy
from test_behavior_channels_pin_refresh import review_source
from test_conversation_transfer_startup import read_startup, saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from hosted_versions_stub import deployed_versions


@pytest.fixture
def peer_runtime(synthetic_standing_v1, monkeypatch):
    c = synthetic_standing_v1
    a = c.h.composition
    policy = peer_policy(c.h.config, c.control.policy)
    original = c.h.config.targets[0]
    deployed_versions(monkeypatch, {original.agent_id: original.agent_version, policy.agent_id: "47"})
    register_agent_scope(a.runtime.store, a.config, policy, c.human, now=c.h.time)
    actor = c.h.config.identity.auth().actors[policy.template.agent_oid]
    c.h.table.backing.apply("Behavior", [("create", {
        "PartitionKey": actor.team, "RowKey": "work:" + policy.profile.legacy_source_work,
        "Payload": json.dumps({"work_id": policy.profile.legacy_source_work, "team": actor.team,
                              "owner": actor.actor, "status": "published", "revision": 1}).encode(),
    })])
    review_source(a.runtime.store, SimpleNamespace(identity=a.config.identity,
        bindings=(policy.template,), policies=(SimpleNamespace(policy=policy.legacy_template_policy),),
        assistant=SimpleNamespace(profiles=(policy.profile,)), review_policy_version=a.config.review_policy_version),
        review_id="b-original-source-review")
    before = read_startup(c)
    b = a.for_agent(policy.agent_id)
    return SimpleNamespace(c=c, a=a, b=b, policy=policy, before=before)


def test_registered_peer_gets_its_own_c1_runtime_without_replacing_a_or_the_shared_ledger(peer_runtime):
    c, a, b, policy, before = (getattr(peer_runtime, name) for name in ("c", "a", "b", "policy", "before"))
    assert b is not a and b.config is a.config
    assert b.runtime is not a.runtime and b.runtime.store is a.runtime.store
    assert b.runtime.dep.workers.contract.capacity == a.runtime.dep.workers.contract.capacity == 1
    assert b.runtime.dep.native_launches.targets == (a.config.targets[1],)
    assert b.runtime.work_policy.policy == policy
    assert b.runtime.work_policy.cost_key == c.control.cost_key
    assert a.runtime.work_policy is c.control and a.runtime.dep.native_launches.targets == a.config.active_targets
    assert a.for_agent(policy.agent_id) is b
    assert a.for_agent(a.config.targets[0].agent_id) is a
    assert read_startup(c).entity_sha256 == before.entity_sha256
    assert c.sessions.calls == []


def test_unregistered_or_unknown_peer_never_falls_back_to_a_runtime(synthetic_standing_v1):
    c = synthetic_standing_v1
    before = read_startup(c)
    for identifier in (c.h.config.targets[1].agent_id, "unknown-agent"):
        with pytest.raises(Problem):
            c.h.composition.for_agent(identifier)
    assert read_startup(c).entity_sha256 == before.entity_sha256


def test_peer_registration_keeps_a_unchanged_and_remains_readable_on_restart(peer_runtime):
    c, a, b, policy = (getattr(peer_runtime, name) for name in ("c", "a", "b", "policy"))
    command = InstructionSubmit(request_id="peer-first-turn", content="Synthetic B-only work.")
    original_a = a.runtime.work_policy
    record = b.runtime.work_policy.register(c.human, command.request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)
    assert record.binding.agent_oid == policy.template.agent_oid
    assert record.binding.owner.root_budget_id == c.control.policy.template.owner.root_budget_id
    assert record.agent_scope == policy.agent_scope
    assert a.runtime.work_policy is original_a and record.binding.binding_id not in a.runtime.bindings
    b.runtime.work_policy.refresh()
    assert b.runtime.work_policy.records[record.binding.binding_id] == record
    read_startup(c)


def test_two_agents_can_register_the_same_request_id_against_one_cost_observation(peer_runtime):
    c, a, b = (getattr(peer_runtime, name) for name in ("c", "a", "b"))
    command = InstructionSubmit(request_id="shared-human-request", content="Synthetic independently owned work.")
    records = [composition.runtime.work_policy.register(c.human, command.request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)
        for composition in (b, a)]
    assert records[0].binding.job_id != records[1].binding.job_id
    assert records[0].binding.owner.root_budget_id == records[1].binding.owner.root_budget_id
    for composition in (b, a):
        composition.runtime.work_policy.observe_cost(c.control.cost_reader(), instruction=True)
    after = read_startup(c)
    assert sum(row.payload["data"].get("kind") == "steady_cumulative_cost_ledger"
               for row in after.rows.values()) == 1


def test_peer_current_policy_reads_its_own_profile_selection(peer_runtime):
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from test_conversation_profile_selection import template
    c, b = peer_runtime.c, peer_runtime.b
    selected = select_profile(b.runtime.work_policy, b.assistant.authority, c.human,
        SelectConversationProfile(request_id="b-profile", expected_selection_id=None, template=template()))
    command = InstructionSubmit(request_id="b-selected-work", content="Synthetic B selected profile.")
    record = b.runtime.work_policy.register(c.human, command.request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)
    assert record.profile_selection_id == selected.payload["result"]["selection_id"]
    binding, _, _ = b.runtime.authorize(record.binding.binding_id, c.human, "read")
    assert binding == record.binding
    read_startup(c)


def test_peer_activation_and_native_target_follow_the_binding_owner(peer_runtime):
    from shared_context.behavior_work_target import activate_target
    c, b = peer_runtime.c, peer_runtime.b
    activate_target(b)
    command = InstructionSubmit(request_id="b-target-work", content="Synthetic B target.")
    record = b.runtime.work_policy.register(c.human, command.request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)
    native = b.runtime.dep.native_launches
    targets = native.targets_for(record.binding.job_id)
    assert len(targets) == 1 and targets[0].agent_oid == record.binding.owner.principal_id
    assert targets[0].agent_id == c.h.config.targets[1].agent_id
    original = native.targets
    try:
        native.targets = c.h.config.active_targets
        with pytest.raises(Problem):
            native.targets_for(record.binding.job_id)
    finally:
        native.targets = original


def test_peer_native_start_reserves_only_its_own_target_and_original_work(peer_runtime):
    c, a, b = peer_runtime.c, peer_runtime.a, peer_runtime.b
    binding, status = b.runtime.work_policy.submit(c.human,
        InstructionSubmit(request_id="b-native-start", content="Synthetic native B turn."))
    assert status.state == "queued" and binding.agent_oid == peer_runtime.policy.template.agent_oid
    native, workers = b.runtime.dep.native_launches, b.runtime.dep.workers
    reserved = []
    original = workers.adapter
    workers.adapter = replace(original, start=lambda start: reserved.append(native.prepare(start)),
                              verify_readiness=lambda _: c.h.bundle.worker)
    try:
        workers.start(binding.job_id, binding.owner)
    finally:
        workers.adapter = original
    assert len(reserved) == 1
    launch, permit = reserved[0]
    assert permit is not None
    assert launch.target.agent_oid == binding.owner.principal_id == c.h.config.targets[1].agent_oid
    assert launch.api_binding_id == binding.binding_id
    assert b.runtime.dep.workers.contract.capacity == 1
    assert a.runtime.dep.native_launches.targets == a.config.active_targets and c.sessions.calls == []
    read_startup(c)


def test_peer_native_claim_saved_result_and_forward_stay_in_b_scope(peer_runtime, monkeypatch):
    from shared_context import behavior_assistant_wire as aw
    from shared_context.behavior_instruction_cost import record_usage
    from shared_context.source_materials import digest
    c, b = peer_runtime.c, peer_runtime.b
    b.assistant.artifacts.store = c.authority.artifacts.store
    peer = SimpleNamespace(
        h=SimpleNamespace(runtime=b.runtime, config=c.h.config, time=c.h.time),
        control=b.runtime.work_policy, authority=b.assistant.authority,
        native=b.runtime.dep.native_launches, human=c.human)
    original = b.assistant.authority.save_text_reply
    def model_then_save(session, command, principal):
        body = {"model": "gpt-4.1-mini", "stream": True,
                "messages": [{"role": "user", "content": "Synthetic B model request."}]}
        identity = digest(body)
        b.assistant.authority.reserve_model(session, aw.Reservation(request_id=identity, request_sha256=identity), principal)
        b.assistant.authority.consume_model(session, aw.ModelRequest(request_id=identity, body=body), principal)
        record_usage(b.assistant.authority, session, identity, (13, 7))
        return original(session, command, principal)
    monkeypatch.setattr(b.assistant.authority, "save_text_reply", model_then_save)
    finished = saved_turn(peer, "b-complete-turn")
    assert b.runtime.job_status(finished.binding.binding_id, c.human).state == "succeeded"
    assert finished.binding.agent_oid == c.h.config.targets[1].agent_oid
    assert finished.binding.owner.root_budget_id == c.control.policy.template.owner.root_budget_id
    assert finished.binding.binding_id not in c.h.runtime.bindings
    read_startup(c)


def test_new_b_conversation_follows_until_its_build_version_without_affecting_a_or_old_b(peer_runtime, monkeypatch):
    from shared_context.behavior_work_target import instruction_hosted_version
    c, b = peer_runtime.c, peer_runtime.b
    original = c.h.config.targets[0]
    deployed_versions(monkeypatch, {original.agent_id: "41"})
    conversation = SimpleNamespace(hosted_agent_version="43")
    with pytest.raises(Problem) as denied:
        instruction_hosted_version(b.runtime.work_policy, conversation, existing=False)
    assert denied.value.body["error"]["code"] == "AGENT_VERSION_FOLLOWING"
    assert instruction_hosted_version(b.runtime.work_policy, conversation, existing=True) == "43"
    assert instruction_hosted_version(c.control, conversation, existing=False) == "41"


def test_b_new_version_is_captured_without_borrowing_a_version(peer_runtime, monkeypatch):
    from shared_context.behavior_work_target import instruction_hosted_version
    c, b = peer_runtime.c, peer_runtime.b
    deployed_versions(monkeypatch, {c.h.config.targets[0].agent_id: "999", b.agent_id: "47"})
    assert instruction_hosted_version(b.runtime.work_policy, None, existing=False) == "47"
    assert instruction_hosted_version(c.control, None, existing=False) == "999"


def test_real_b_admission_and_cold_refresh_capture_only_its_saved_memory_settings(peer_runtime):
    from shared_context.memory_bridge_settings import MemoryBridgeSettingsService
    from test_memory_agent_scope import command as memory_command
    c, a, b = peer_runtime.c, peer_runtime.a, peer_runtime.b
    aid = MemoryBridgeSettingsService(a.runtime).select(c.human, memory_command()).payload["result"]["settings_id"]
    service = MemoryBridgeSettingsService(b.runtime)
    bid = service.select(c.human, memory_command()).payload["result"]["settings_id"]
    command = InstructionSubmit(request_id="b-own-memory", content="Capture only B's own settings.")
    record = b.runtime.work_policy.register(c.human, command.request_id,
        hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command)
    assert record.profile.memory_scope.settings_id == bid and bid != aid
    service.select(c.human, memory_command("b-now-disabled", previous=bid, enabled=False))
    b.runtime.work_policy.records.clear()
    b.runtime.work_policy.refresh()
    restored = b.runtime.work_policy.records[record.binding.binding_id]
    assert restored == record and restored.profile.memory_scope.enabled
    read_startup(c)


def test_a_and_b_model_usage_share_one_nonresetting_ledger(peer_runtime, monkeypatch):
    from shared_context import behavior_assistant_wire as aw
    from shared_context.behavior_instruction_cost import record_usage
    from shared_context.source_materials import digest
    from shared_context.behavior_work_policy import CostLedger
    c, b = peer_runtime.c, peer_runtime.b
    b.assistant.artifacts.store = c.authority.artifacts.store
    peer = SimpleNamespace(h=SimpleNamespace(runtime=b.runtime, config=c.h.config, time=c.h.time),
        control=b.runtime.work_policy, authority=b.assistant.authority,
        native=b.runtime.dep.native_launches, human=c.human)
    for case, name in ((peer, "b-metered"), (c, "a-metered")):
        authority = case.authority
        original = authority.save_text_reply
        def save(session, command, principal, *, authority=authority, original=original, name=name):
            body = {"model": "gpt-4.1-mini", "stream": True,
                    "messages": [{"role": "user", "content": name}]}
            identity = digest(body)
            authority.reserve_model(session, aw.Reservation(request_id=identity, request_sha256=identity), principal)
            authority.consume_model(session, aw.ModelRequest(request_id=identity, body=body), principal)
            record_usage(authority, session, identity, (13, 7))
            return original(session, command, principal)
        monkeypatch.setattr(authority, "save_text_reply", save)
        saved_turn(case, name)
        read_startup(c)
    inventory = read_startup(c)
    ledgers = [row.payload["data"] for row in inventory.rows.values()
               if row.payload["data"].get("kind") == "steady_cumulative_cost_ledger"]
    costs = [row.payload["data"] for row in inventory.rows.values()
             if row.payload["data"].get("kind") == "instruction_model_cost"]
    assert len(ledgers) == 1 and len(costs) == 2
    assert len({cost["job_id"] for cost in costs}) == 2
    current = CostLedger.model_validate_json(json.dumps(ledgers[0]))
    assert current.model_pending_microusd == 0 and current.model_metered_microusd > 0
