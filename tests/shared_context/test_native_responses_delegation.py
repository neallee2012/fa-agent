"""D0: public Hosted Responses ingress, real N1/SDK, scripted model HTTP only."""

import json
import socket
from importlib.metadata import version

from copilot import CopilotClient
import httpx

from shared_context import m2_copilot
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeReceipt, NativeResult
from shared_context.native_delegation import NativeDelegationReply
from shared_context.native_sdk import NativeSdkExecutor, native_capability_providers
from sdk_checkpoint_local import sse
from test_m2_copilot import runtime_path
from test_native_delegation import TASK, delegate_request
from test_native_responses import responses
from test_native_turns import IDENTITY, local_env
from test_skill_isolation_native import child_environment


def test_responses_parent_keeps_its_original_sdk_child_across_reentry_and_followup(
    responses, monkeypatch, tmp_path, runtime_path,
):
    import _socket

    assert version("github-copilot-sdk") == "1.0.13"

    def loopback_only(sock, address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}
        return _socket.socket.connect(sock, address)

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)
    rid, cid, call_id = "d0-responses-parent", "d0-responses-conversation", "d0-original-child-call"
    child_text, final_text = "Child verified the supplied sample: 530.", "Parent integrated the original child: 530."
    model_url = "https://offline.services.ai.azure.com/api/projects/offline/openai/v1"
    requests, child_results = [], []

    def provider(request):
        assert str(request.url) == model_url + "/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        number = len(requests)
        assert number <= 4, "A repeated response must not run another model or child"
        tools = {tool["function"]["name"] for tool in body.get("tools", [])}
        if number == 1:
            assert "task" in tools
            delta = {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                    "function": {"name": "task", "arguments": json.dumps(TASK)}}]}
        elif number == 2:
            assert not tools
            assert TASK["prompt"] in json.dumps(body["messages"])
            delta = {"content": child_text}
        elif number == 3:
            assert "task" in tools
            message = next(value for value in reversed(body["messages"]) if value["role"] == "tool")
            assert message["tool_call_id"] == call_id
            text = message["content"]
            if isinstance(text, list):
                text = "".join(part["text"] for part in text if part.get("type") == "text")
            result = NativeDelegationReply.model_validate_json(text)
            assert (result.agent_id, result.conversation_id, result.parent_request_id, result.tool_call_id) == (
                IDENTITY.agent_id, cid, rid, call_id)
            assert result.status == "completed" and result.result == child_text
            assert result.child_agent_id is not None and result.child_agent_id != result.sdk_session_id
            child_results.append(result)
            delta = {"content": final_text}
        else:
            assert final_text in json.dumps(body["messages"])
            delta = {"content": "Continued without redispatching the earlier child."}
        return sse(request, delta, number)

    executor = NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
        model_url=model_url, token_provider=lambda: "OFFLINE-native-model-token",
        model_transport=httpx.MockTransport(provider),
        capability_providers=native_capability_providers("https://api.offline.invalid", lambda: "OFFLINE-api-token"),
    )
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    values = delegate_request(rid).model_dump(mode="json")
    values.update(state_profile="foundry-responses-v1", new_conversation_id=cid)
    request = NATIVE_REQUEST.validate_json(json.dumps(values))

    def read_original(identifier):
        response = responses.invoke(NATIVE_REQUEST.validate_json(json.dumps({
            "operation": "read", "request_id": identifier,
        })))
        assert response.status_code == 200, response.text
        return NativeReceipt.model_validate_json(response.content)

    admitted = responses.invoke(request)
    assert admitted.status_code == 202, admitted.text
    accepted = read_original(rid)
    assert accepted.status == "accepted" and accepted.response_id is None and not requests
    assert accepted.state_profile == "foundry-responses-v1" and accepted.capture == request.capture
    completed_response = responses.respond(accepted, stream=True)
    assert completed_response.status_code == 200, completed_response.text
    completed = read_original(rid)
    assert completed.status == "completed" and completed.response_id is not None
    assert completed.command == request.command and completed.turn_number == 1
    assert completed.usage.usage[0].requests == 3 and len(requests) == 3
    assert len(child_results) == 1
    original_answer = responses.invoke(NativeResult(conversation_id=cid, request_id=rid))
    assert original_answer.status_code == 200 and original_answer.json()["text"] == final_text

    following = request.model_dump(mode="json")
    following.pop("new_conversation_id", None)
    following["command"].update(request_id="d0-followup", conversation_id=cid, content="Continue the original result.")
    assert responses.invoke(NATIVE_REQUEST.validate_json(json.dumps(following))).status_code == 202
    assert responses.respond(read_original("d0-followup"), stream=True).status_code == 200
    continued = read_original("d0-followup")
    assert continued.status == "completed" and continued.turn_number == 2
    assert continued.usage.usage[0].requests == 1 and len(requests) == 4

    repeated = responses.invoke(request)
    assert repeated.status_code == 202 and NativeReceipt.model_validate_json(repeated.content) == completed
    assert responses.respond(accepted, stream=True).status_code == 200
    assert read_original(rid) == completed and read_original("d0-followup") == continued
    original_conversation = responses.invoke(NATIVE_REQUEST.validate_json(json.dumps({
        "operation": "conversation", "conversation_id": cid,
    })))
    assert original_conversation.status_code == 200
    assert original_conversation.json()["last_request_id"] == "d0-followup"
    assert len(requests) == 4 and len(child_results) == 1
