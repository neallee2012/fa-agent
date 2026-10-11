"""The shared Foundry transport must acknowledge the exact Agent request, not another valid receipt."""

import json

import pytest

from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NativeAgentReceive, NativeReceipt
from shared_context.errors import Problem
from test_native_turn_api import native_api
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_native_turn_wire import agent_receive


@pytest.mark.parametrize("changed", ["request", "source", "conversation", "session"])
def test_agent_receive_never_accepts_an_unrelated_self_consistent_receipt(native_api, changed):
    case = native_api
    request = agent_receive("original-peer-input", "original-cid", case.foundry.target.agent_id)
    values = request.model_dump(mode="json")
    sid = case.foundry.target.session_id
    if changed == "request":
        values["command"]["request_id"] = values["source"]["message_id"] = "another-request"
    elif changed == "source":
        values["source"]["sender"]["agent_id"] = "another-sender"
    elif changed == "conversation":
        values["command"]["conversation_id"] = "another-cid"
    else:
        sid = "another-session"
    other = NativeAgentReceive.model_validate_json(json.dumps(values))
    response = NativeReceipt(
        agent_id=case.foundry.target.agent_id, hosted_session_id=sid,
        request_id=other.command.request_id, conversation_id=other.command.conversation_id,
        command=other.command, capture=other.capture, source=other.source,
        submit_digest=other.sha256, capture_digest=other.capture.sha256, status="accepted")
    case.remote.reply_override = lambda _command: response
    with pytest.raises(Problem):
        NativeTurnGateway._invoke(case.foundry, request)
    assert len(case.remote.calls) == 1
