"""B1b metadata/key seams only; no second runtime, SDK, model or cloud writes."""

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_work_policy import SteadyWorkPolicy, WorkPolicyControl, derive_work
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant_server import assistant_configuration
from test_conversation_transfer_startup import read_startup, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from hosted_versions_stub import deployed_versions


def legacy_policy(config):
    return SteadyWorkPolicy(
        policy_id="synthetic-b1b-legacy", revision=1, configuration_sha256=config.sha256,
        evidence_mode="native", authority_domain=config.table.scope.authority_domain,
        control_binding=config.workers.control_binding, active=True, read_enabled=True,
        template=config.bindings[0], profile=config.assistant.profiles[0],
        legacy_template_policy=config.policies[0].policy,
        cost_scope_sha256=digest(config.resource_bindings()),
        cost_observer_oid=config.evidence.observer_oid, minimum_observed_microusd=1_000_000,
        work_estimate_microusd=1_000_000, cost_max_age_seconds=300,
        queue_seconds=min(600, config.delivery.max_queue_seconds), max_model_requests=1,
    )


def test_server_resolves_agent_identity_by_oid_without_changing_original_config():
    raw = assistant_configuration()
    raw["identity"]["agents"].reverse()
    config = ServerConfiguration.model_validate_json(json.dumps(raw))
    original = config.model_dump_json(), config.sha256, config.resource_bindings()

    a = config.agent_scope(config.targets[0].agent_id)
    b = config.agent_scope(config.targets[1].agent_id)

    assert a.legacy_keys is True and b.legacy_keys is False
    assert a.target == config.targets[0] and b.target == config.targets[1]
    assert b.identity.oid == b.target.agent_oid
    assert b.identity.actor == next(agent.actor for agent in config.identity.agents
                                    if agent.oid == config.targets[1].agent_oid)
    assert a.identity != b.identity
    assert (a.tenant_id, a.team_id, a.human_oid, a.authority_domain) == (
        b.tenant_id, b.team_id, b.human_oid, b.authority_domain)
    assert a.configuration_sha256 == b.configuration_sha256 == config.sha256
    assert (config.model_dump_json(), config.sha256, config.resource_bindings()) == original
    assert config.active_targets == (config.targets[0],)
    with pytest.raises(Problem) as denied:
        config.agent_scope("not-a-configured-agent")
    assert denied.value.body["error"]["code"] == "AGENT_SCOPE_DENIED"


def peer_policy(config, original):
    from shared_context.behavior_work_policy import AgentScopeReference
    target = config.targets[1]
    template = original.template.model_copy(update={
        "binding_id": "synthetic-b-template", "agent_oid": target.agent_oid,
        "owner": original.template.owner.model_copy(update={
            "owner_record_id": "synthetic-b-owner", "conversation_id": "synthetic-b-conversation",
            "policy_id": "synthetic-b-operation-policy", "principal_id": target.agent_oid,
        }),
        "event_id": "synthetic-b-event", "job_id": "synthetic-b-job",
        "work": original.template.work.model_copy(update={"work_id": "synthetic-b-work"}),
        "operation_id": "synthetic-b-operation", "attempt_id": "synthetic-b-attempt",
        "grant_id": "synthetic-b-grant", "subject": "synthetic-b-subject",
        "source_ids": ("synthetic-b-source",), "claim_ids": ("synthetic-b-claim",),
    })
    profile = original.profile.model_copy(update={
        "binding_id": template.binding_id, "claim_id": "synthetic-b-claim",
        "source": original.profile.source.model_copy(update={"source_id": "synthetic-b-source"}),
        "legacy_source_work": "synthetic-b-source-work", "artifact_id": "synthetic-b-artifact",
        "memory_scope": None,
    })
    policy = original.model_copy(update={
        "policy_id": "synthetic-b-policy", "template": template, "profile": profile,
        "legacy_template_policy": original.legacy_template_policy.model_copy(update={
            "binding_digest": digest(["fa-behavior-api-v1", original.evidence_mode, template.model_dump(mode="json")]),
        }),
        "agent_scope": AgentScopeReference(registry_id="synthetic-agents-v1", agent_id=target.agent_id),
    })
    return SteadyWorkPolicy.model_validate_json(policy.model_dump_json())


def test_legacy_literals_stay_fixed_while_peer_work_and_conversation_keys_are_isolated():
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_instruction import conversation_id, conversation_key, hosted_session_id, sdk_session_id
    config = ServerConfiguration.model_validate_json(json.dumps(assistant_configuration()))
    a = legacy_policy(config)
    command = InstructionSubmit(request_id="b1b-fixed-request", content="Synthetic instruction.")
    admission, binding, profile = derive_work(a, command.request_id, "native", instruction=command)
    assert hashlib.sha256(config.model_dump_json().encode()).hexdigest() == "def67e6aee18d4b7834de2705a33aedfdd3e27771661a56f344d3ab0eb637375"
    assert config.sha256 == "8201c0d9723072109ec8bb7700b347964936b5c9e287c32b3669a6b2d927dcbe"
    assert hashlib.sha256(a.model_dump_json().encode()).hexdigest() == "c36742ecb1d36339e84c9a4d2df6068ecf55d692884236427121b08803b85373"
    assert hashlib.sha256(binding.model_dump_json().encode()).hexdigest() == "c4f66220b98789151f6f8fa701fede7ea79c1c92b38168197cd52bacb7a0adab"
    assert hashlib.sha256(profile.model_dump_json().encode()).hexdigest() == "0f18a959e6451d7069a32de17cc0af7bc52967a045207e7ba25bcc1997b3791c"
    assert admission == "311de55bea104abfdd00735f8711c6a9372610ecc9667081d50eda7aa4a50358"
    cid = conversation_id(a, command)
    assert cid == "b5ca39c58c1e7697c8d77fe00f733568be6caca811c79a5ec120a239d123918d"
    assert conversation_key(cid) == "d9b6362423d2dfa33035635ecad51e90afacfb31b151fb92c9a4048790b3be3b"
    assert sdk_session_id(cid) == "602fe2ceebeb2b17ac6097c4ff2d79f3912549ad7ce8e99f6f597327a98678dd"
    assert hosted_session_id(cid) == "fa-chat-68882e3871b8543ac34e8053b6fe817179efbc8b8bbfba98"
    control_a = object.__new__(WorkPolicyControl)
    control_a.policy = a
    assert (control_a.policy_key, control_a.registry_key, control_a.revocation_key, control_a.cost_key) == (
        "954d0a0d69f6a13a6d5b7ab4dab60146787c6917d3d09baae4535634a5082787",
        "a36d19601cb6c0c18628e5608f94117d355f3a675b0de5bf594d95342d6e0d0c",
        "ea05c9a1557afdf97110b53a8c4b79298a5eaf5f4ac399cfe702da848b8c5230",
        "44143e56569ce06223ae2a4292977aa3a51b56f679a63e6f3537541c1048bb44",
    )

    b = peer_policy(config, a)
    control_b = object.__new__(WorkPolicyControl)
    control_b.policy = b
    peer_admission, peer_binding, peer_profile = derive_work(b, command.request_id, "native", instruction=command)
    assert peer_admission != admission and peer_binding.agent_oid == config.targets[1].agent_oid
    assert peer_binding.binding_id != binding.binding_id and peer_binding.job_id != binding.job_id
    assert peer_profile.artifact_id != profile.artifact_id
    assert peer_profile.instruction.conversation_id != cid
    assert sdk_session_id(peer_profile.instruction.conversation_id) != sdk_session_id(cid)
    assert hosted_session_id(peer_profile.instruction.conversation_id) != hosted_session_id(cid)
    assert control_a.policy_key != control_b.policy_key
    assert control_a.registry_key != control_b.registry_key
    assert control_a.revocation_key != control_b.revocation_key
    assert control_a.cost_key == control_b.cost_key
    assert peer_binding.owner.root_budget_id == binding.owner.root_budget_id
    assert peer_binding.root_request_id == binding.root_request_id


def test_peer_registry_installs_without_rewriting_legacy_pins_or_initializing_another_ledger(synthetic_standing_v1):
    from shared_context.behavior_agent_registry import register_agent_scope, resolve_agent_policy
    c = synthetic_standing_v1
    config, store = c.h.config, c.h.runtime.store
    before = read_startup(c)
    policy = peer_policy(config, c.control.policy)
    result = register_agent_scope(store, config, policy, c.human, now=c.h.time)

    assert result.payload["result"] == {
        "registry_id": "synthetic-agents-v1", "agent_id": config.targets[1].agent_id,
        "cost_ledger_id": c.control.cost_key, "runtime_activated": False, "budget_reset": False,
    }
    assert resolve_agent_policy(store, config, config.targets[0].agent_id) == c.control.policy
    assert resolve_agent_policy(store, config, config.targets[1].agent_id) == policy
    for key, row in before.rows.items():
        assert store.read_snapshot((key,))[key] == row
    assert c.h.runtime.work_policy is c.control
    assert c.native.targets == config.active_targets == (config.targets[0],)
    assert c.sessions.calls == []
    assert register_agent_scope(store, config, policy, c.human, now=c.h.time) == result


def test_new_registry_passes_real_standing_and_current_runtime_startup_readers(synthetic_standing_v1):
    from shared_context.behavior_agent_registry import register_agent_scope
    c = synthetic_standing_v1
    before = read_startup(c)
    policy = peer_policy(c.h.config, c.control.policy)
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    after = read_startup(c)
    added = set(after.rows) - set(before.rows)
    assert len(added) == 2
    assert {after.rows[key].payload["data"]["kind"] for key in added} == {
        "agent_scope_registry", "steady_work_registry",
    }
    assert sum(row.payload["data"].get("kind") == "steady_cumulative_cost_ledger"
               for row in after.rows.values()) == 1
    assert all(after.rows[key] == row for key, row in before.rows.items())


def test_registered_peer_cannot_attach_to_a_runtime_or_initialize_another_cost_ledger(synthetic_standing_v1):
    from shared_context.behavior_agent_registry import register_agent_scope
    from shared_context.behavior_work_policy import attach_standing_policy
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    control = WorkPolicyControl(c.h.runtime, policy, lambda: pytest.fail("scope metadata cannot refresh cost"))
    assert control.installed() == policy
    before = read_startup(c)
    for action in (
        control.attach,
        control.refresh,
        lambda: control.register(c.human, "peer-request", "a" * 64),
        lambda: control.observe_cost(c.control.cost_reader()),
        lambda: attach_standing_policy(c.h.composition, agent_id=policy.agent_id),
    ):
        with pytest.raises(Problem) as denied:
            action()
        assert denied.value.body["error"]["code"] == "AGENT_RUNTIME_SCOPE_NOT_WIRED"
    for action in (control.initial_records, control.install_offline):
        with pytest.raises(Problem):
            action(c.human, "no-second-ledger", c.h.time + timedelta(minutes=5))
    assert read_startup(c).entity_sha256 == before.entity_sha256
    assert c.h.runtime.work_policy is c.control


def test_peer_target_and_version_do_not_fall_back_to_a_selection_or_global_environment(synthetic_standing_v1, monkeypatch):
    from shared_context.behavior_agent_registry import register_agent_scope
    from shared_context.behavior_work_target import instruction_hosted_version, selected_targets, target_key
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    control = WorkPolicyControl(c.h.runtime, policy, c.control.cost_reader)
    assert target_key(policy.authority_domain) == "a272c8fff2258a7bd1f049637a617581b7b10620275d5c711d4d3e233d042fe2"
    assert target_key(policy.authority_domain, policy.agent_id) != target_key(policy.authority_domain)
    assert selected_targets(c.h.runtime.store, c.h.config) == c.h.config.active_targets
    assert selected_targets(c.h.runtime.store, c.h.config, agent_id=policy.agent_id) == (c.h.config.targets[1],)
    deployed_versions(monkeypatch, {c.h.config.targets[0].agent_id: "999", policy.agent_id: "47"})
    conversation = SimpleNamespace(hosted_agent_version="7")
    assert instruction_hosted_version(c.control, conversation, existing=False) == "999"
    assert instruction_hosted_version(control, conversation, existing=False) == "47"
    monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSION", "not-a-version")
    monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSIONS", "not-json")
    assert instruction_hosted_version(control, conversation, existing=True) == "7"
    assert instruction_hosted_version(control, conversation, existing=False) == "47"
    assert c.native.targets == c.h.config.active_targets


def test_peer_profile_selection_uses_its_own_head_and_keeps_a_selection_bytes(synthetic_standing_v1):
    from shared_context.behavior_agent_registry import register_agent_scope
    from shared_context.behavior_profile_selection import (
        SelectConversationProfile, current_selection, head_key, select_profile, selection_key,
    )
    from test_conversation_profile_selection import template
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    control = WorkPolicyControl(c.h.runtime, policy, c.control.cost_reader)
    command = SelectConversationProfile(request_id="same-choice-id", expected_selection_id=None, template=template())
    original = select_profile(c.control, c.authority, c.human, command)
    akey = head_key(policy.authority_domain)
    assert akey == "ec60c98fd3e282c02a5f00589f52634513ef4d6b3fad6880e1ac314e34f55b63"
    assert selection_key(policy.authority_domain, "fixed-choice") == "0f3f83d65abc48e26f0ad982dff6488283054d9b93d963cb7684ac6122f94187"
    saved = c.h.runtime.store.read_snapshot((akey,))[akey]
    choice = command.model_copy(update={"template": template(text_attachment_profile="fa-source-text-attachments-v1")})
    selected = select_profile(control, c.authority, c.human, choice)
    assert selected.payload["result"]["selection_id"] != original.payload["result"]["selection_id"]
    assert c.h.runtime.store.read_snapshot((akey,))[akey] == saved
    assert current_selection(c.h.runtime.store, c.control.policy).command == command
    assert current_selection(c.h.runtime.store, policy).command == choice
    assert select_profile(control, c.authority, c.human, choice, reconcile=True) == selected
    read_startup(c)


def test_history_keys_rebuild_the_saved_peer_scope_not_current_environment(monkeypatch):
    from datetime import datetime, timezone
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_work_policy import PerWorkRuntimePolicy, WorkRegistration, derive_registered_work
    config = ServerConfiguration.model_validate_json(json.dumps(assistant_configuration()))
    policy = peer_policy(config, legacy_policy(config))
    command = InstructionSubmit(request_id="saved-peer-turn", content="Keep this original identity.")
    admission, binding, profile = derive_work(policy, command.request_id, "native", instruction=command,
                                              hosted_agent_version="13")
    runtime_policy = PerWorkRuntimePolicy(**{
        **policy.legacy_template_policy.model_dump(),
        "binding_digest": digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")]),
        "policy_id": policy.policy_id, "policy_revision": policy.revision, "admission_id": admission,
        "job_id": binding.job_id, "stage": "queued", "accepted_at": datetime(2026, 9, 29, tzinfo=timezone.utc),
        "queue_deadline": None, "valid_until": None, "instruction": True,
    })
    record = WorkRegistration(
        admission_id=admission, request_id=command.request_id, submit_sha256="a" * 64,
        policy_id=policy.policy_id, policy_revision=policy.revision, policy_sha256=policy.sha256,
        principal_oid=policy.template.human_oid, binding=binding, profile=profile, runtime_policy=runtime_policy,
        agent_scope=policy.agent_scope,
    )
    saved = WorkRegistration.model_validate_json(record.model_dump_json())
    monkeypatch.setenv("FA_INSTRUCTION_HOSTED_VERSION", "999")
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", "not-a-supported-default")
    assert derive_registered_work(policy, saved, "native") == (admission, binding, profile)
    current = object.__new__(WorkPolicyControl)
    current.policy = policy
    history = object.__new__(WorkPolicyControl)
    history.policy = SimpleNamespace(authority_domain=policy.authority_domain)
    history.records = {binding.binding_id: saved}
    assert (history.policy_key, history.registry_key, history.revocation_key, history.cost_key) == (
        current.policy_key, current.registry_key, current.revocation_key, current.cost_key)
    from shared_context.behavior_profile_selection import selection_key
    history.records[binding.binding_id] = saved.model_copy(update={"profile_selection_id": "saved-peer-choice"})
    assert selection_key(policy.authority_domain, "saved-peer-choice", policy.agent_id) in history.gate_keys(binding)
    assert selection_key(policy.authority_domain, "saved-peer-choice") not in history.gate_keys(binding)
    with pytest.raises(Problem) as denied:
        derive_registered_work(policy, saved.model_copy(update={"agent_scope": None}), "native")
    assert denied.value.body["error"]["code"] == "WORK_POLICY_REGISTRY_INVALID"


def test_peer_conversation_cannot_reuse_a_cid_archive_or_private_memory_settings(synthetic_standing_v1, monkeypatch):
    from shared_context.behavior_agent_registry import register_agent_scope
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_instruction import instruction_state
    from shared_context.behavior_work_policy import configured_prompt_override
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    control = WorkPolicyControl(c.h.runtime, policy, c.control.cost_reader)
    command = InstructionSubmit(request_id="same-human-request", content="Only the original assistant owns this.")
    record = c.control.register(c.human, command.request_id, hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
                                instruction=command)
    before = read_startup(c)
    current, row = instruction_state(control, command)
    assert row is None and current.agent_oid == policy.template.agent_oid
    assert current.conversation_id != record.profile.instruction.conversation_id
    assert current.archive_id is None and current.active_job_id is None and current.jobs == ()
    with pytest.raises(Problem) as denied:
        instruction_state(control, command.model_copy(update={"conversation_id": record.profile.instruction.conversation_id}))
    assert denied.value.body["error"]["code"] == "CONVERSATION_SCOPE_DENIED"
    with pytest.raises(Problem) as pending:
        instruction_state(c.control, command)
    assert pending.value.body["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"
    captured = control.conversation_memory(c.human, current)
    from shared_context.memory_bridge_settings import settings_head_key
    assert captured.value is None and captured.predicates == {
        settings_head_key(policy.authority_domain, policy.agent_scope): None}
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", "not-a-supported-default")
    assert configured_prompt_override(policy) is None
    assert read_startup(c).entity_sha256 == before.entity_sha256
    assert c.sessions.calls == []


def test_registry_unknown_reconciles_original_receipt_without_resend_or_new_ids(synthetic_standing_v1):
    from shared_context.behavior_agent_registry import register_agent_scope
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    before = read_startup(c)
    with pytest.raises(Problem) as absent:
        register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time, reconcile=True)
    assert absent.value.body["error"]["code"] == "AGENT_SCOPE_UNKNOWN"
    assert read_startup(c).entity_sha256 == before.entity_sha256
    c.h.table.after_apply = "lost"
    with pytest.raises(Problem) as unknown:
        register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    sent = sum(request.method == "POST" for request in c.h.table.requests)
    c.h.table.after_apply = None
    receipt = register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time, reconcile=True)
    assert register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time) == receipt
    changed = policy.model_copy(update={
        "agent_scope": policy.agent_scope.model_copy(update={"registry_id": "not-a-retry-with-another-id"}),
    })
    with pytest.raises(Problem) as conflict:
        register_agent_scope(c.h.runtime.store, c.h.config, changed, c.human, now=c.h.time)
    assert conflict.value.body["error"]["code"] == "AGENT_SCOPE_CONFLICT"
    assert sum(request.method == "POST" for request in c.h.table.requests) == sent
    after = read_startup(c)
    assert all(after.rows[key] == row for key, row in before.rows.items())


def test_peer_registry_rejects_wrong_identity_budget_scope_and_guard_conflicts(synthetic_standing_v1):
    from dataclasses import replace
    from shared_context.behavior_agent_registry import register_agent_scope
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    before = read_startup(c)
    for principal in (
        replace(c.human, oid=c.h.config.targets[1].agent_oid, kind="agent", actor_id="agent-b"),
        replace(c.human, team="another-team"),
        replace(c.human, actions=frozenset({"read"})),
    ):
        with pytest.raises(Problem):
            register_agent_scope(c.h.runtime.store, c.h.config, policy, principal, now=c.h.time)
    for candidate in (
        policy.model_copy(update={"cost_scope_sha256": "a" * 64}),
        policy.model_copy(update={"agent_scope": policy.agent_scope.model_copy(update={
            "agent_id": c.h.config.targets[0].agent_id,
        })}),
    ):
        with pytest.raises(Problem):
            register_agent_scope(c.h.runtime.store, c.h.config, candidate, c.human, now=c.h.time)
    c.h.table.conflict_status = 412
    with pytest.raises(Problem) as conflict:
        register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    assert conflict.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
    c.h.table.conflict_status = None
    assert read_startup(c).entity_sha256 == before.entity_sha256


def test_peer_revocation_does_not_revoke_a_or_change_the_shared_ledger(synthetic_standing_v1):
    from shared_context.behavior_agent_registry import register_agent_scope
    c = synthetic_standing_v1
    policy = peer_policy(c.h.config, c.control.policy)
    register_agent_scope(c.h.runtime.store, c.h.config, policy, c.human, now=c.h.time)
    control = WorkPolicyControl(c.h.runtime, policy, c.control.cost_reader)
    before = read_startup(c)
    revoked = control.revoke(c.human, "revoke-only-peer")
    assert control.revoke(c.human, "revoke-only-peer") == revoked
    assert not control.installed().active and not control.installed().read_enabled
    assert c.control.installed() == c.control.policy
    after = read_startup(c)
    assert set(after.rows) - set(before.rows) == {control.revocation_key}
    assert all(after.rows[key] == row for key, row in before.rows.items())
