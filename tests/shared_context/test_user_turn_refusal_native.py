"""Real SDK/authority read refusal feedback; responses are scripted, not model quality."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from sdk_checkpoint_local import sse
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_user_turn import UserTurnRead
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import signing_key, jwk_client
from test_user_turn_read import ORIGINAL, configured


@pytest.mark.parametrize("fault", [None, "upstream-scope", "other-conversation", "wrong-turn"],
                         ids=["scope", "upstream-scope", "other-conversation", "wrong-turn"])
def test_native_scope_refusal_is_safe_actionable_and_keeps_the_original_work(assistant, monkeypatch, fault):
    h, control, human = configured(assistant)
    phase, calls, feedback, reads, client_errors = 0, [], [], [], []
    if fault:
        original = h.assistant.read_user_turn
        def corrupt_response(session, command, principal):
            if fault == "upstream-scope":
                raise Problem(403, "USER_TURN_SCOPE_DENIED", "PRIVATE_UPSTREAM_REFUSAL")
            result = original(session, command, principal)
            updates = ({"conversation_id": "PRIVATE_WRONG_CONVERSATION"} if fault == "other-conversation"
                       else {"turn_number": 17})
            return result.model_copy(update={"source": result.source.model_copy(update=updates)})
        monkeypatch.setattr(h.assistant, "read_user_turn", corrupt_response)
        read = AssistantClient.read_user_turn
        def observe(client, number):
            try:
                return read(client, number)
            except Problem as error:
                client_errors.append(error.body["error"]["code"])
                raise
        monkeypatch.setattr(AssistantClient, "read_user_turn", observe)

    def text(message):
        value = message["content"]
        return value if isinstance(value, str) else "".join(part["text"] for part in value)

    def tool(turn, identity):
        return {"tool_calls": [{"index": 0, "id": identity, "type": "function",
                               "function": {"name": "read_user_turn",
                                            "arguments": json.dumps({"turn_number": turn})}}]}

    def provider(request):
        body = json.loads(request.content)
        count = sum(value == phase for value in calls)
        if phase == 0:
            delta = {"content": "Original user turn was saved."}
        elif count == 0:
            delta = tool(1 if fault else 99, "outside-current-history")
        elif count == 1:
            result = [message for message in body["messages"] if message["role"] == "tool"][-1]
            assert result["tool_call_id"] == "outside-current-history"
            feedback.append(text(result))
            delta = ({"content": "Original read was refused; no substitute was used."} if fault
                     else tool(1, "permitted-original"))
        else:
            result = [message for message in body["messages"] if message["role"] == "tool"][-1]
            assert result["tool_call_id"] == "permitted-original"
            original = UserTurnRead.model_validate_json(text(result))
            assert original.text == ORIGINAL and original.source.turn_number == 1
            reads.append(original.source)
            delta = {"content": "Read the permitted original; no external operation performed."}
        calls.append(phase)
        return sse(request, delta, len(calls))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="PRIVATE_SYNTHETIC_PROVIDER_TOKEN")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    conversation = None
    bindings = []
    with TemporaryDirectory(prefix="turn-feedback-") as directory:
        for phase, prompt in enumerate((ORIGINAL, "Read the permitted original user turn.")):
            h.executor.root = Path(directory) / str(phase)
            accepted, _ = submit(h, f"refusal-feedback-{phase}", prompt, conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            bindings.append(binding)
            completed = h.runtime.controller.step(binding.binding_id)
            assert completed.state == "succeeded" and completed.result_forwarded
            assert not completed.obligation_fulfilled
    assert calls == ([0, 1, 1] if fault else [0, 1, 1, 1])
    assert len(reads) == (0 if fault else 1)
    assert len({binding.job_id for binding in bindings}) == 2
    assert len(feedback) == 1
    assert "Authority refused" in feedback[0]
    if fault and fault != "upstream-scope":
        assert feedback == ["Authority refused the action; do not resend unknown effects or claim completion."]
        assert client_errors == ["USER_TURN_SOURCE_INVALID"]
    else:
        assert "USER_TURN_SCOPE_DENIED" in feedback[0]
        assert "completed past turn" in feedback[0]
        if fault:
            assert client_errors == ["USER_TURN_SCOPE_DENIED"]
    assert "unknown effects" in feedback[0]
    assert "PRIVATE_SYNTHETIC_PROVIDER_TOKEN" not in feedback[0]
    assert "PRIVATE_WRONG_CONVERSATION" not in feedback[0]
    assert "PRIVATE_UPSTREAM_REFUSAL" not in feedback[0]
    assert ORIGINAL not in feedback[0]
    projection = control.conversation(human, conversation)
    public = json.dumps([turn["progress"] for turn in projection["turns"]])
    assert "USER_TURN_SCOPE_DENIED" not in public
    assert "PRIVATE_SYNTHETIC_PROVIDER_TOKEN" not in public and ORIGINAL not in public
