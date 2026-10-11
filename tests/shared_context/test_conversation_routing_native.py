"""Fixed SDK with offline provider transport; no model quality or cloud claims."""

import importlib.metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
from copilot import CopilotClient
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_work_policy_api import install_work_policy_routes
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_conversation_routing import conversation_view, read_reply
from test_instruction_flow import instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled


def test_fixed_sdk_keeps_interleaved_conversations_and_follow_up_separate(assistant, monkeypatch):
    h, _, _, _ = enabled(assistant)
    alpha_text = "ROUTING_ALPHA_QUESTION"
    bravo_text = "ROUTING_BRAVO_QUESTION"
    follow_text = "ROUTING_ALPHA_FOLLOW_UP"
    alpha_answer = "ROUTING_ALPHA_ANSWER"
    bravo_answer = "ROUTING_BRAVO_ANSWER"
    follow_answer = "ROUTING_ALPHA_FOLLOW_UP_ANSWER"
    expected = [(bravo_text, bravo_answer), (alpha_text, alpha_answer), (follow_text, follow_answer)]
    requests, sends, stopped, forced, runtime_versions = [], [], [], [], []
    original_send = CopilotSession.send
    original_stop = CopilotClient.stop
    original_force_stop = CopilotClient.force_stop
    original_status = CopilotClient.get_status

    async def observe_send(session, prompt, **options):
        sends.append((session.session_id, prompt))
        return await original_send(session, prompt, **options)

    async def observe_stop(client, *args, **kwargs):
        result = await original_stop(client, *args, **kwargs)
        stopped.append(True)
        return result

    async def observe_force_stop(client, *args, **kwargs):
        forced.append(True)
        return await original_force_stop(client, *args, **kwargs)

    async def observe_status(client, *args, **kwargs):
        result = await original_status(client, *args, **kwargs)
        runtime_versions.append(result.version)
        return result

    monkeypatch.setattr(CopilotSession, "send", observe_send)
    monkeypatch.setattr(CopilotClient, "stop", observe_stop)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)
    monkeypatch.setattr(CopilotClient, "get_status", observe_status)

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        question, answer = expected[len(requests)]
        body = json.loads(request.content)
        messages = json.dumps(body["messages"])
        assert question in messages
        if question == bravo_text:
            assert alpha_text not in messages and alpha_answer not in messages
        elif question == alpha_text:
            assert bravo_text not in messages and bravo_answer not in messages
        else:
            assert alpha_text in messages and alpha_answer in messages
            assert bravo_text not in messages and bravo_answer not in messages
        requests.append({"ordinal": len(requests) + 1, "correct_context": True})
        return sse(request, {"content": answer}, len(requests))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_ROUTING_CREDENTIAL")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None

    with TemporaryDirectory(prefix="fa-routing-") as directory:
        root = Path(directory)
        alpha, _ = submit(h, "native-alpha", alpha_text)
        bravo, _ = submit(h, "native-bravo", bravo_text)
        for index, accepted in enumerate((bravo, alpha), 1):
            h.executor.root = root / str(index)
            result = h.runtime.controller.step(accepted["binding_id"])
            assert result.state == "succeeded" and result.result_forwarded
            assert not result.obligation_fulfilled
        first_alpha = read_reply(h, alpha).content
        first_bravo = read_reply(h, bravo).content
        assert read_reply(h, alpha).json()["text"] == alpha_answer
        assert read_reply(h, bravo).json()["text"] == bravo_answer

        h.executor.root = root / "3"
        follow_up, _ = submit(h, "native-alpha-follow-up", follow_text, alpha["conversation_id"])
        assert h.runtime.controller.step(follow_up["binding_id"]).state == "succeeded"
        assert read_reply(h, follow_up).json()["text"] == follow_answer
        repeated, _ = submit(h, "native-bravo", bravo_text)
        assert repeated["binding_id"] == bravo["binding_id"]
        assert repeated["conversation_id"] == bravo["conversation_id"]
        assert read_reply(h, alpha).content == first_alpha
        assert read_reply(h, bravo).content == first_bravo
        assert len(conversation_view(h, alpha)["turns"]) == 2
        assert len(conversation_view(h, bravo)["turns"]) == 1

    assert [prompt for _, prompt in sends] == [bravo_text, alpha_text, follow_text]
    assert sends[0][0] != sends[1][0] == sends[2][0]
    assert len(requests) == len(stopped) == 3 and forced == []
    assert runtime_versions and set(runtime_versions) == {"1.0.83"}
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    if output := os.environ.get("FA_CONVERSATION_ROUTING_EVIDENCE_DIR"):
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "conversation-routing-native.json").write_text(json.dumps({
            "evidence": "Fixed SDK, real FA HTTP authority, explicit offline provider transport.",
            "sdk_version": "1.0.13",
            "runtime_versions": runtime_versions,
            "model_requests": requests,
            "sdk_user_text_exact": True,
            "independent_conversation_sessions": True,
            "original_follow_up_session": True,
            "prior_reply_bytes_unchanged": True,
            "duplicate_submit_model_requests": 0,
            "normal_sdk_stop_returns": len(stopped),
            "force_stop_calls": len(forced),
            "cloud_requests": 0,
            "paid_model_requests": 0,
            "limitations": [
                "Scripted outputs are routing evidence, not semantic quality.",
                "Client receipt replay is covered separately by SDK I/O-double tests.",
                "No Azure storage, deployed Hosted environment or unsupported channel acceptance.",
            ],
        }, indent=2) + "\n", encoding="utf-8")
