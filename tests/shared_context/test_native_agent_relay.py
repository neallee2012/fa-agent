"""Native Agent messages enter the same durable RID/chain surface, never a human submission."""

from dataclasses import replace
import hashlib
import json

import pytest

from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_native_turn_wire import NativeReceipt, NativeResultReference
from shared_context.errors import Problem
from test_behavior_server import synthetic_configuration
from test_native_turns import IDENTITY, harness, local_env, native_retry_policy
from test_native_turn_wire import capture, submit


def sender_case():
    config = synthetic_configuration()
    sender_scope, recipient = (config.agent_scope(target.agent_id) for target in config.targets[:2])
    principal = BehaviorPrincipal(
        str(sender_scope.tenant_id), str(sender_scope.identity.oid), sender_scope.team_id,
        "agent", sender_scope.identity.actor, frozenset({"read", "submit", "execute"}))
    original = submit("sender-rid", text="Ask another Agent to inspect the public sample.")
    sender = NativeReceipt(state_profile=original.state_profile,
        agent_id=sender_scope.target.agent_id, hosted_session_id="original-host-session",
        request_id=original.command.request_id, conversation_id="original-human-cid",
        submit_digest=original.sha256, capture_digest=original.capture.sha256,
        command=original.command, capture=original.capture, status="started", turn_number=1,
    )
    return config, sender_scope, recipient, principal, sender


def test_agent_message_uses_verified_sender_and_recipient_capture_without_human_authorization():
    from shared_context.native_agent_relay import NativePeerRequest, prepare_request

    _, scope, recipient, principal, sender = sender_case()
    selected = capture()
    command = NativePeerRequest(
        recipient_agent_id=recipient.target.agent_id, kind="request", content="Inspect the public sample.")
    message = prepare_request(
        principal, scope, sender, recipient, selected, command, tool_call_id="original-sdk-call")
    assert message.operation == "agent_receive"
    assert message.command.authorize_own_result is False
    assert message.command.request_id == message.source.message_id
    assert message.command.conversation_id is None
    assert message.command.content == "Inspect the public sample."
    assert message.capture == selected
    assert message.source.sender.agent_id == scope.target.agent_id
    assert message.source.sender.conversation_id == "original-human-cid"
    assert message.source.sender.request_id == "sender-rid"
    assert message.source.recipient_agent_id == recipient.target.agent_id
    assert message.source.hop == 1 and message.source.root == message.source.sender
    assert message.source.kind == "agent_request" and message.source.result is None
    assert prepare_request(principal, scope, sender, recipient, selected, command,
                           tool_call_id="original-sdk-call") == message


def test_verified_registry_shaped_recipient_accepts_once_and_queues_behind_its_active_turn(harness):
    from shared_context.native_agent_relay import NativePeerRequest, prepare_request

    _, scope, recipient, principal, sender = sender_case()
    recipient = recipient.model_copy(update={
        "target": recipient.target.model_copy(update={"agent_id": IDENTITY.agent_id}), "legacy_keys": False})
    harness.sdk.block = True
    assert harness.invoke(submit("receiver-busy")).status_code == 202
    harness.wait("receiver-busy", "started")
    message = prepare_request(
        principal, scope, sender, recipient, capture(),
        NativePeerRequest(recipient_agent_id=IDENTITY.agent_id, kind="handoff", content="Inspect this queued sample."),
        tool_call_id="original-handoff-call")
    response = harness.invoke(message)
    assert response.status_code == 202, response.text
    accepted = NativeReceipt.model_validate_json(response.content)
    assert accepted.status == "accepted" and accepted.source == message.source
    repeated = harness.invoke(message)
    assert repeated.status_code == 202
    assert repeated.json()["conversation_id"] == accepted.conversation_id
    assert len(harness.sdk.calls) == 1
    harness.sdk.block = False
    harness.wait("receiver-busy", "completed")
    done = harness.wait(accepted.request_id, "completed")
    assert done.source == message.source and done.command.authorize_own_result is False
    assert [call[0] for call in harness.sdk.calls] == ["receiver-busy", message.command.request_id]
    assert harness.sdk.max_active == 1


@pytest.mark.parametrize("case", ["self", "team", "tenant", "human", "oid", "receipt", "ended"])
def test_native_relay_refuses_untrusted_sender_or_cross_scope_before_receiving(case):
    from shared_context.native_agent_relay import NativePeerRequest, prepare_request

    _, scope, recipient, principal, sender = sender_case()
    if case == "self":
        recipient = scope
    elif case == "team":
        recipient = recipient.model_copy(update={"team_id": "different-team"})
    elif case == "tenant":
        principal = replace(principal, tenant_id="ffffffff-ffff-ffff-ffff-ffffffffffff")
    elif case == "human":
        principal = replace(principal, kind="human", actor_id=None)
    elif case == "oid":
        principal = replace(principal, oid="ffffffff-ffff-ffff-ffff-ffffffffffff")
    elif case == "receipt":
        sender = sender.model_copy(update={"agent_id": recipient.target.agent_id})
    else:
        sender = sender.model_copy(update={"status": "unknown"})
    with pytest.raises(Problem) as refused:
        prepare_request(principal, scope, sender, recipient, capture(),
            NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Public sample."),
            tool_call_id="trusted-callback")
    assert refused.value.status == 403


def test_completed_peer_result_wakes_the_original_cid_with_the_original_request_message_id():
    from shared_context.native_agent_relay import NativePeerRequest, prepare_request, prepare_result

    _, scope, recipient, principal, sender = sender_case()
    request = prepare_request(
        principal, scope, sender, recipient, capture(),
        NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Check the sample."),
        tool_call_id="original-call")
    text = "The original peer result is 530."
    reference = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="a" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=len(text.encode()),
        text_parts=1, checkpoint_sha256="b" * 64)
    completed = NativeReceipt(state_profile=request.state_profile,
        agent_id=recipient.target.agent_id, hosted_session_id="peer-host-session",
        request_id=request.command.request_id, conversation_id="peer-cid",
        submit_digest=request.sha256, capture_digest=request.capture.sha256,
        command=request.command, capture=request.capture, source=request.source,
        status="completed", turn_number=1, result=reference)
    peer_principal = replace(principal, oid=str(recipient.identity.oid), actor_id=recipient.identity.actor)
    wake = prepare_result(peer_principal, recipient, completed, scope, sender.capture, reference, text)
    assert wake.command.request_id == request.source.message_id == wake.source.in_reply_to
    assert wake.command.request_id != sender.request_id
    assert wake.command.conversation_id == sender.conversation_id
    assert wake.source.sender.request_id == completed.request_id and wake.source.sender.conversation_id == "peer-cid"
    assert wake.source.kind == "agent_result" and wake.source.result == reference
    assert wake.source.root_id == request.source.root_id and wake.source.hop == 0
    assert wake.command.content == text and wake.command.authorize_own_result is False
    assert prepare_result(peer_principal, recipient, completed, scope, sender.capture, reference, text) == wake


def test_repeated_wake_enters_one_new_turn_in_the_original_a_conversation(harness):
    from shared_context.native_agent_relay import NativePeerRequest, prepare_request, prepare_result

    _, scope, recipient, principal, _ = sender_case()
    scope = scope.model_copy(update={"target": scope.target.model_copy(update={"agent_id": IDENTITY.agent_id})})
    harness.sdk.block = True
    assert harness.invoke(submit("a-original")).status_code == 202
    active = harness.wait("a-original", "started")
    sent = prepare_request(
        principal, scope, active, recipient, capture(),
        NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Verify 530."),
        tool_call_id="call-that-handed-off")
    harness.sdk.block = False
    original = harness.wait("a-original", "completed")
    text = "Verified: 530."
    reference = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="c" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=len(text.encode()),
        text_parts=1, checkpoint_sha256="d" * 64)
    completed = NativeReceipt(state_profile=sent.state_profile,
        agent_id=recipient.target.agent_id, hosted_session_id="peer-host-session",
        request_id=sent.command.request_id, conversation_id="peer-conversation",
        command=sent.command, capture=sent.capture, source=sent.source,
        submit_digest=sent.sha256, capture_digest=sent.capture.sha256,
        status="completed", turn_number=1, result=reference)
    peer = replace(principal, oid=str(recipient.identity.oid), actor_id=recipient.identity.actor)
    wake = prepare_result(peer, recipient, completed, scope, original.capture, reference, text)
    for _ in range(3):
        response = harness.invoke(wake)
        assert response.status_code == 202, response.text
        assert response.json()["conversation_id"] == original.conversation_id
    resumed = harness.wait(wake.command.request_id, "completed")
    assert resumed.turn_number == 2 and resumed.source == wake.source
    assert [call[0] for call in harness.sdk.calls] == ["a-original", wake.command.request_id]
    assert harness.sdk.calls[1][2] is not None
    changed = wake.model_copy(update={"command": wake.command.model_copy(update={"content": "A different result"})})
    refused = harness.invoke(changed)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert len(harness.sdk.calls) == 2


def test_native_relay_keeps_the_original_root_without_a_two_hop_limit():
    from shared_context.native_agent_relay import NativePeerRequest, prepare_request
    from shared_context.behavior_native_turn_wire import NativeAgentReceive
    from test_native_turn_wire import agent_receive

    _, scope, recipient, principal, _ = sender_case()
    values = agent_receive("inherited-message", recipient=scope.target.agent_id).model_dump(mode="json")
    values["source"]["hop"] = 2
    inherited = NativeAgentReceive.model_validate_json(json.dumps(values))
    sender = NativeReceipt(state_profile=inherited.state_profile,
        agent_id=scope.target.agent_id, hosted_session_id="own-native-session",
        request_id=inherited.command.request_id, conversation_id="receiving-cid",
        command=inherited.command, capture=inherited.capture, source=inherited.source,
        submit_digest=inherited.sha256, capture_digest=inherited.capture.sha256,
        status="started", turn_number=1)
    onward = prepare_request(principal, scope, sender, recipient, capture(), NativePeerRequest(
        recipient_agent_id=recipient.target.agent_id, kind="request", content="Continue the original shared task."),
        tool_call_id="next-original-call")
    assert onward.source.hop == 3
    assert onward.source.root_id == inherited.source.root_id and onward.source.root == inherited.source.root
