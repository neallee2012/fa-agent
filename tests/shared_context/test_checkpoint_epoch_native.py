"""Fixed SDK with a paused compaction provider, real admission and checkpoint readers.

Only the model/MCP transports are synthetic. No cloud or paid model is used.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_capability_prompt import assemble_capabilities
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_instruction_models import InstructionModel
from shared_context.behavior_sdk_checkpoint import SdkCheckpointStore, checkpoint_key
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_sdk_state import CheckpointProfile
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_capability_capture import CATALOG, FILES, GRANTS, WEB_TOOLS, fake_services, refs
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import check_history, instruction_models
from test_instruction_prompt import saved_turn
from test_instruction_responses_native import response_bytes
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import checkpoint_profile


TEXTS = (
    "  Original instruction: project=Cedar-47; delivery=2026-10-08; count=7.\r\nKeep the exact spelling.",
    "  Continue the same task using the earlier source.\r\nDo not change the dates.",
    "  Correction: delivery=2026-10-10; count=8.\r\nKeep the original line as historical data.",
)
ANSWERS = ("Original delivery 2026-10-08, count 7.", "Original task retained.", "Effective delivery 2026-10-10, count 8.")
SUMMARY = (
    "<overview>Project Cedar-47.</overview>\n"
    "<history>Original delivery=2026-10-08; count=7. This summary is historical data.</history>\n"
    "<checkpoint_title>Original task</checkpoint_title>"
)


def message_text(content):
    if isinstance(content, str):
        return content
    assert isinstance(content, list)
    assert all(part.get("type") in {"text", "input_text"} and isinstance(part.get("text"), str) for part in content)
    return "".join(part["text"] for part in content)


@pytest.mark.parametrize("provider_name,revoke_during_compaction", [
    (None, False), ("openai-responses", False), (None, True),
], ids=["completions", "responses", "revoked-completions"])
def test_paused_compaction_preserves_followup_and_refreshes_next_prefix(
    assistant, monkeypatch, tmp_path, runtime_path, provider_name, revoke_during_compaction,
):
    h, control, human = configured(assistant)
    h.runtime.capability_grants, h.runtime.capability_catalog = GRANTS, CATALOG
    service_calls = fake_services(monkeypatch)
    model = "gpt-4.1-mini" if provider_name is None else "gpt-5.4-mini"
    if provider_name is not None:
        control.models.models[model] = InstructionModel(
            id=model, display_name=model, max_output_tokens=32768, supports_reasoning_summary=True,
            input_microusd_per_million=400000, output_microusd_per_million=1600000,
        )
    preset = checkpoint_profile()
    if provider_name is not None:
        preset = CheckpointProfile(platform=preset.platform, provider=provider_name)
    chosen, _ = select(h, request_id="epoch-selection", value=template(
        HISTORY_PROFILE, checkpoint_profile=preset, model_provider=provider_name,
        user_text_file_profile=FILES, capabilities=refs("WebIQ-MCP"),
    ))
    assert chosen.status_code == 200, chosen.text
    selection_id = chosen.json()["result"]["selection_id"]
    compact_entered, compact_released = Event(), Event()
    requests = []
    phase = 0
    expected_prefix = None
    expected_tools = None
    current_command = None
    provider_errors = []

    def respond(request):
        assert str(request.url).endswith("/responses" if provider_name else "/chat/completions")
        body = json.loads(request.content)
        number = len(requests) + 1
        requests.append({"phase": phase, "ordinal": number})
        if number == 2:
            compact_entered.set()
            assert compact_released.wait(30), "test must release its one compaction response"
            text = SUMMARY
        else:
            if provider_name is None:
                system = [item["content"] for item in body["messages"] if item["role"] == "system"]
                tools = {item["function"]["name"] for item in body.get("tools", [])}
                user = [item["content"] for item in body["messages"] if item["role"] == "user"]
            else:
                system = [message_text(body["instructions"])] if body.get("instructions") else [
                    message_text(item["content"]) for item in body["input"] if item.get("role") == "system"
                ]
                tools = {item["name"] for item in body.get("tools", [])}
                user = [message_text(item["content"]) for item in body["input"] if item.get("role") == "user"]
                assert body["store"] is False and not body.get("previous_response_id")
            assert system == [expected_prefix], "model-facing system prefix differs from captured assembly"
            assert tools == expected_tools, (tools, expected_tools)
            # The runtime may prepend current_datetime, but the accepted user text stays verbatim.
            assert user[-1].endswith(current_command.content)
            requests[-1]["prefix_sha256"] = hashlib.sha256(expected_prefix.encode()).hexdigest()
            if phase == 2:
                assert not set(WEB_TOOLS) & tools
                assert "2026-10-08" in json.dumps(body) and "2026-10-10" in json.dumps(body)
            text = ANSWERS[phase]
        if provider_name is not None:
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content=response_bytes(number, text=text))
        return sse(request, {"content": text}, number)

    def provider(request):
        try:
            return respond(request)
        except AssertionError as error:
            provider_errors.append(error)
            raise

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_EPOCH_ONLY")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None,
    ))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    cid = None
    saved = []
    rejected = None
    try:
        for phase, text in enumerate(TEXTS[:2] if revoke_during_compaction else TEXTS):
            h.executor.root = Path(tmp_path) / f"epoch-{phase}"
            current_command = InstructionSubmit(request_id=f"epoch-turn-{phase}", content=text,
                                                conversation_id=cid, model=model)
            accepted = h.client.post("/work-policy/v1/submit", content=current_command.model_dump_json(),
                                     headers=h.headers())
            assert accepted.status_code == 200, accepted.text
            cid, binding_id = accepted.json()["conversation_id"], accepted.json()["binding_id"]
            task = control.records[binding_id].profile.instruction
            assembled = assemble_capabilities(task.prompt_profile, task.capabilities)
            expected_prefix = (assembled.system_message if assembled else assemble_prompt(task.prompt_profile).system_message)
            expected_tools = set(assemble_prompt(task.prompt_profile).record.declared_capabilities)
            if phase < 2:
                expected_tools.update(WEB_TOOLS)
            if phase == 1:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    running = pool.submit(h.runtime.controller.step, binding_id)
                    try:
                        assert compact_entered.wait(30), "native manual compaction did not reach its transport"
                        pending = InstructionSubmit(request_id="epoch-turn-2", content=TEXTS[2], conversation_id=cid, model=model)
                        rejected = h.client.post("/work-policy/v1/submit", content=pending.model_dump_json(),
                                                 headers=h.headers())
                        assert rejected.status_code == 409
                        assert rejected.json()["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"
                        prior = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/1/reply", headers=h.headers())
                        assert prior.content == saved[0]["reply"]
                        current = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/2/context", headers=h.headers())
                        assert current.status_code == 404
                        assert current.json()["error"]["code"] == "TURN_CONTEXT_NOT_READY"
                        if revoke_during_compaction:
                            revoked = control.revoke(human, "revoke-while-compaction-is-pending")
                            assert revoked.payload["result"]["revoked"] is True
                    finally:
                        compact_released.set()
                    if revoke_during_compaction:
                        with pytest.raises(Problem):
                            running.result(timeout=45)
                        if provider_errors:
                            raise provider_errors[0]
                        assert len(requests) == 2
                        job_id = h.runtime.bindings[binding_id].job_id
                        assert h.store.read_snapshot((checkpoint_key(job_id),))[checkpoint_key(job_id)] is None
                        assert h.store.read_snapshot((key("text-archive", job_id),))[key("text-archive", job_id)] is None
                        assert saved_turn(h, saved[0]["job_id"]) == saved[0]["turn_archive"]
                        unavailable = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/2/context", headers=h.headers())
                        assert unavailable.status_code == 403
                        return
                    outcome = running.result(timeout=45)
            else:
                outcome = h.runtime.controller.step(binding_id)
            if provider_errors:
                raise provider_errors[0]
            assert outcome.state == "succeeded" and outcome.result_forwarded
            view = h.client.get("/work-policy/v1/conversations/" + cid, headers=h.headers())
            assert view.status_code == 200 and view.json()["can_follow_up"] is True
            context = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/{phase + 1}/context", headers=h.headers())
            assert context.status_code == 200, context.text
            details = context.json()
            assert details["prompt_assembly"]["prefix_sha256"] == assemble_prompt(HISTORY_PROFILE).record.prefix_sha256
            reply = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/{phase + 1}/reply", headers=h.headers())
            assert reply.status_code == 200 and reply.json()["text"] == ANSWERS[phase]
            binding = h.runtime.bindings[binding_id]
            archive = saved_turn(h, binding.job_id)
            state = SdkCheckpointStore(h.artifacts.store).load(
                archive.archive, binding, conversation_id=cid, sdk_session_id=archive.archive.sdk_session_id,
                job_id=binding.job_id, checkpoint_profile=preset, model_provider=provider_name,
            )
            assert [event["data"]["content"] for event in events_from(state.files)
                    if event["type"] == "user.message"] == list(TEXTS[:phase + 1])
            saved.append({"reply": reply.content, "context": details, "job_id": binding.job_id, "turn_archive": archive})
            if phase == 1:
                narrower, _ = select(h, request_id="epoch-narrow", previous=selection_id, value=template(
                    HISTORY_PROFILE, checkpoint_profile=preset, model_provider=provider_name,
                    user_text_file_profile=FILES, capabilities=(),
                ))
                assert narrower.status_code == 200, narrower.text
        assert rejected is not None and len(requests) == 4
        assert [item["phase"] for item in requests] == [0, 1, 1, 2]
        assert saved[0]["context"]["compaction"] is None
        initial, inherited = saved[1]["context"]["compaction"], saved[2]["context"]["compaction"]
        assert initial["source"] == "stored-checkpoint-observation"
        assert inherited["source"] == "inherited-checkpoint-observation"
        assert initial["observation_job_id"] == saved[1]["job_id"]
        assert inherited["observation_job_id"] == initial["observation_job_id"]
        assert inherited["summary_sha256"] == initial["summary_sha256"]
        assert initial["checkpoint_number"] == inherited["checkpoint_number"] == 1
        assert initial["previous_state_sha256"] == saved[0]["context"]["archive"]["sha256"]
        assert inherited["previous_state_sha256"] == saved[1]["context"]["archive"]["sha256"]
        assert saved[2]["context"].get("capability_assembly") is None
        assert requests[0]["prefix_sha256"] == requests[2]["prefix_sha256"]
        assert requests[3]["prefix_sha256"] != requests[2]["prefix_sha256"]
        assert service_calls == []
        check_history(h)
    finally:
        compact_released.set()
