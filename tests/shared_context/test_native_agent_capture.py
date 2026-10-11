"""The native relay resolves the receiver's own settings without impersonating its human owner."""

from dataclasses import replace

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_native_turn_wire import NativeAgentCommand
from shared_context.errors import Problem
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


def test_verified_agent_capture_reads_receiver_settings_without_a_human_or_new_job(peer_runtime, monkeypatch):
    from shared_context.behavior_native_capture import resolve_agent_capture, resolve_capture

    case = peer_runtime
    source = case.a.config.agent_scope(case.a.config.targets[0].agent_id)
    target = case.b.config.agent_scope(case.b.agent_id)
    principal = BehaviorPrincipal(
        str(source.tenant_id), str(source.identity.oid), source.team_id, "agent", source.identity.actor,
        frozenset({"read", "submit", "execute"}))
    command = NativeAgentCommand(request_id="original-peer-input", content="Read the original public sample.")
    control, authority = case.b.runtime.work_policy, case.b.assistant.authority
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from test_conversation_profile_selection import template
    select_profile(control, authority, case.c.human, SelectConversationProfile(
        request_id="choose-native-peer", expected_selection_id=None, template=template("fa-instruction-v2")))
    before = control.store._guard()
    monkeypatch.setattr(control.store, "_submit", lambda *_args, **_kwargs: pytest.fail("capture must not write Table"))
    captured = resolve_agent_capture(control, authority, principal, target, command)
    assert captured.model == control.models.select(None)
    assert captured.template.role.profile == "fa-instruction-v2"
    assert control.records == {} and control.store._guard() == before
    with pytest.raises(Problem) as human_only:
        resolve_capture(control, authority, principal, InstructionSubmit(
            request_id=command.request_id, content=command.content))
    assert human_only.value.status == 403
    with pytest.raises(Problem) as cross_team:
        resolve_agent_capture(control, authority, replace(principal, team="other-team"), target, command)
    assert cross_team.value.status == 403
