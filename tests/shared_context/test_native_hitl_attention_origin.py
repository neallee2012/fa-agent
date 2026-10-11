"""Original relay writer and scoped native reads verify grouping; no model, SDK process or live requests."""

import copy

import pytest

from shared_context.behavior_native_turn_wire import NativeTurnAddress
from shared_context.native_agent_api import NativeAgentRelayGateway
from shared_context.native_hitl_attention import owner_origin_reader
from test_native_agent_api import (
    instruction_models, jwk_client, peer_runtime, relay_case, send_command, signing_key, synthetic_standing_v1,
)


def test_original_b_relay_groups_under_a_root_without_new_writes(relay_case, monkeypatch):
    case = relay_case
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    accepted = gateway.send(case.principal, send_command(case))
    b = case.remotes[1].records[accepted.message_id]
    address = NativeTurnAddress(
        agent_id=b.agent_id, conversation_id=b.conversation_id, request_id=b.request_id)
    origin = owner_origin_reader(case.base.a, case.base.c.human)
    saved, quota = copy.deepcopy(case.remotes[1].records), copy.deepcopy(case.repository.data)
    guard = case.base.a.runtime.store._guard()
    before = [len(remote.calls) for remote in case.remotes]
    resolved = origin(address)
    assert resolved.state == "verified"
    assert resolved.root == resolved.sender == b.source.root == b.source.sender
    assert (resolved.root.agent_id, resolved.root.conversation_id, resolved.root.request_id) == (
        case.sender.agent_id, case.sender.conversation_id, case.sender.request_id)
    counts = [len(remote.calls) for remote in case.remotes]
    assert origin(address) == resolved
    assert [len(remote.calls) for remote in case.remotes] == counts
    for remote, start in zip(case.remotes, before):
        assert {command.operation for _request, command in remote.calls[start:]} <= {"read", "session_read"}
    assert case.remotes[1].records == saved and case.repository.data == quota
    assert case.base.a.runtime.store._guard() == guard


def test_direct_human_turn_uses_its_own_verified_address(relay_case, monkeypatch):
    case = relay_case
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    address = NativeTurnAddress(
        agent_id=case.sender.agent_id, conversation_id=case.sender.conversation_id, request_id=case.sender.request_id)
    resolved = owner_origin_reader(case.base.a, case.base.c.human)(address)
    assert resolved.state == "verified" and resolved.root == address and resolved.sender is None


@pytest.mark.parametrize("damage", ["root_team", "root_id", "sender", "recipient", "unreadable", "cid"])
def test_unverified_sources_never_guess_an_a_root(relay_case, monkeypatch, caplog, damage):
    case = relay_case
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    accepted = gateway.send(case.principal, send_command(case))
    original = case.remotes[1].records[accepted.message_id]
    source = original.source
    if damage == "root_team":
        source = source.model_copy(update={"root": source.root.model_copy(update={"agent_id": "another-team-agent"})})
    elif damage == "root_id":
        source = source.model_copy(update={"root_id": "wrong-original-root"})
    elif damage == "sender":
        source = source.model_copy(update={"sender": source.sender.model_copy(update={"conversation_id": "wrong-cid"})})
    elif damage == "recipient":
        source = source.model_copy(update={"recipient_agent_id": case.sender.agent_id})
    elif damage == "unreadable":
        case.remotes[0].error = (503, "NATIVE_STATE_STORE_UNKNOWN")
    case.remotes[1].records[accepted.message_id] = original.model_copy(update={"source": source})
    address = NativeTurnAddress(
        agent_id=original.agent_id, conversation_id="wrong-cid" if damage == "cid" else original.conversation_id,
        request_id=original.request_id)
    result = owner_origin_reader(case.base.a, case.base.c.human)(address)
    assert result.model_dump(mode="json") == {"state": "unavailable", "root": None, "sender": None}
    assert "NATIVE_HITL_ORIGIN_UNAVAILABLE" in caplog.text
