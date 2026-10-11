"""One fixed SDK turn reaches the real candidate callback through scripted local I/O."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from copilot import CopilotClient
from fastapi.testclient import TestClient
import httpx
import pytest

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted, m2_copilot
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.memory_candidate_wire import CandidateReceipt
from shared_context.memory_candidates import MemoryCandidates
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_isolation_native import child_environment, owned_path
from test_turn_origin_profile_native import ConfinedOriginHandler


@pytest.mark.parametrize("invalid_quote", [False, True])
def test_sdk_candidate_receipt_reaches_the_next_request_then_becomes_reviewable(
        assistant, runtime_path, monkeypatch, invalid_quote):
    h, control, human = configured(assistant)
    h.runtime.capability_grants = available_grants(None, ("fa-memory-candidate-v1",))
    grant = h.runtime.capability_grants["fa-memory-candidate-v1"]
    quote = "This project uses Traditional Chinese."
    counts = {"violations": [], "proxy_requests": 0, "provider_requests": 0, "stage_posts": 0}
    bridges, processes = [], []
    original_init = hosted.AssistantSession.__init__
    original_stage = hosted.AssistantClient.stage_memory_candidate
    original_start = CopilotClient.start
    accepted = None

    with TemporaryDirectory(prefix="fa-mem01-") as directory:
        root = Path(directory)
        env = child_environment(root)

        def isolated_client(**options):
            owned_path(options["base_directory"], root)
            owned_path(options["working_directory"], root)
            return CopilotClient(**{
                **options, "env": dict(env),
                "request_handler": ConfinedOriginHandler(options["request_handler"], counts),
            })

        async def start(client, *args, **options):
            result = await original_start(client, *args, **options)
            process = client._cli_process
            if process not in processes:
                processes.append(process)
            return result

        def bridge(instance, *args, **options):
            original_init(instance, *args, **options)
            bridges.append(instance)

        def stage(client, command):
            counts["stage_posts"] += 1
            return original_stage(client, command)

        def provider(request):
            assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
            counts["provider_requests"] += 1
            number = counts["provider_requests"]
            assert number <= 2
            body = json.loads(request.content)
            assert {tool["function"]["name"] for tool in body["tools"]} == {
                *HISTORY_CAPABILITIES, "stage_memory_candidate"}
            if number == 1:
                delta = {"tool_calls": [{
                    "index": 0, "id": "candidate-once", "type": "function",
                    "function": {"name": "stage_memory_candidate", "arguments": json.dumps({
                        "decision": "candidate", "reason": "stable_preference",
                        "quote": "Not in this user's message." if invalid_quote else quote,
                    })},
                }]}
            else:
                values = [message for message in body["messages"]
                          if message["role"] == "tool" and message.get("tool_call_id") == "candidate-once"]
                assert len(values) == 1
                content = values[0]["content"]
                text = content if isinstance(content, str) else "".join(part.get("text", "") for part in content)
                value = MemoryCandidates(h.runtime).read(accepted["binding_id"], human)
                if invalid_quote:
                    assert "MEMORY_CANDIDATE_SOURCE_INVALID" in text
                    assert value.state == "not_recorded"
                else:
                    receipt = CandidateReceipt.model_validate_json(text)
                    assert receipt.binding_id == accepted["binding_id"] and receipt.effective is False
                    assert value.state == "awaiting_saved_turn"
                assert value.text is None
                delta = {"content": "Suggested for human review; no effective memory was created."}
            return sse(request, delta, number)

        gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
            SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_MEMORY_CANDIDATE")),
            transport=httpx.MockTransport(provider))
        h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
        install_work_policy_routes(h.app, SimpleNamespace(
            runtime=h.runtime, verifier=h.verifier, assistant=SimpleNamespace(authority=h.assistant),
            installation=lambda: None, require_current_native_evidence=lambda: None))
        monkeypatch.setattr(m2_copilot, "CopilotClient", isolated_client)
        monkeypatch.setattr(CopilotClient, "start", start)
        monkeypatch.setattr(hosted.AssistantSession, "__init__", bridge)
        monkeypatch.setattr(hosted.AssistantClient, "stage_memory_candidate", stage)
        try:
            with TestClient(h.app) as client:
                h.client = h.executor.http_client = client
                h.executor.root, h.executor.model_transport = root / "hosted", None
                response, _ = select(h, request_id="candidate-native-selection", value=template(
                    HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1", capabilities=(grant.ref,)))
                assert response.status_code == 200, response.text
                command = InstructionSubmit(request_id="candidate-native-turn",
                                            content="Please suggest remembering: " + quote)
                response = client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
                assert response.status_code == 200, response.text
                accepted = response.json()
                assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
                assert control.read_reply(human, accepted["conversation_id"], 1).text == (
                    "Suggested for human review; no effective memory was created.")
                value = MemoryCandidates(h.runtime).read(accepted["binding_id"], human)
                assert value.state == ("not_recorded" if invalid_quote else "pending_review")
                assert value.text == (None if invalid_quote else quote) and value.effective is False
                assert counts == {"violations": [], "proxy_requests": 2, "provider_requests": 2, "stage_posts": 1}
                assert len(bridges) == len(processes) == 1
                assert bridges[0]._sdk_stop.graceful and bridges[0]._sdk_stop.runtime_version == "1.0.83"
        finally:
            assert all(process is not None and process.poll() is not None for process in processes)
