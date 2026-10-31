"""Actual fixed SDK/custom tool with signed local reviewed-memory authority only."""

import hashlib
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

from sdk_checkpoint_local import sse
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_operations import control_key
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy import work_policy_key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from web_test_console.freeform import ConversationProjection
from test_behavior_api import METRICS
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_reviewed_work_memory import NOTE, PROFILE, configured_memory, reviewed_note

PREFIX_SHA = "1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3"


def test_fixed_sdk_reads_reviewed_team_note_only_on_demand_and_restores_its_exact_sources(
    assistant, monkeypatch,
):
    reviewed_note(assistant)
    h, control, _ = configured_memory(assistant)
    prompt = assemble_prompt(PROFILE)
    assert prompt.record.prefix_sha256 == PREFIX_SHA and prompt.record.prefix_utf8_bytes == 2719
    phase, calls, bridges, configurations, forced, user_sends = 0, [], [], [], [], []
    texts = ("  使用這份已審 Team 工作知識；不要修改或批准任何操作。\r\n",
             "\t再讀同一份已審記憶與來源，不改成搜尋。\r\n")
    initialize = hosted.AssistantSession.__init__
    create, resume, stop, send = CopilotClient.create_session, CopilotClient.resume_session, CopilotClient.force_stop, CopilotSession.send

    def bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def created(instance, **options):
        configurations.append(options)
        return await create(instance, **options)

    async def resumed(instance, sid, **options):
        configurations.append(options)
        return await resume(instance, sid, **options)

    async def force_stop(instance):
        forced.append(True)
        return await stop(instance)

    async def user_send(instance, text, **options):
        user_sends.append(text)
        return await send(instance, text, **options)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", bridge)
    monkeypatch.setattr(CopilotClient, "create_session", created)
    monkeypatch.setattr(CopilotClient, "resume_session", resumed)
    monkeypatch.setattr(CopilotClient, "force_stop", force_stop)
    monkeypatch.setattr(CopilotSession, "send", user_send)
    monkeypatch.setattr(h.runtime, "memory_projection", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("This capability must not call native Memory search/extraction.")))

    def text_result(message):
        content = message["content"]
        return content if isinstance(content, str) else "".join(part["text"] for part in content)

    def tool_call(name, argument, identity):
        return {"tool_calls": [{"index": 0, "id": identity, "type": "function",
                               "function": {"name": name, "arguments": json.dumps(argument)}}]}

    def provider(request):
        packet = json.loads(request.content)
        number = sum(item["turn"] == phase for item in calls) + 1
        tools = {value["function"]["name"]: value["function"] for value in packet["tools"]}
        assert set(tools) == {"read_context", "execute_operation", "read_work_memory"}
        metadata = json.loads(tools["read_work_memory"]["description"].rsplit("\n", 1)[-1])
        assert metadata["schema_version"] == "fa-work-memory-catalog-v1"
        assert metadata["source"]["claim_id"] == "claim-one" and metadata["source"]["claim_version"] == 2
        assert tools["read_work_memory"]["parameters"]["properties"]["claim_id"]["enum"] == ["claim-one"]
        assert "TEAM_MEMORY_CANARY" not in tools["read_work_memory"]["description"]
        assert "reviewer_id" not in tools["read_work_memory"]["description"]
        system = [message["content"] for message in packet["messages"] if message["role"] == "system"]
        assert prompt.system_message in system
        assert all("TEAM_MEMORY_CANARY" not in text for text in system)
        results = [message for message in packet["messages"] if message["role"] == "tool"]
        read_result = (phase == 0 and number == 2) or (phase == 1 and number == 3)
        if phase == 0 and number == 1:
            assert "TEAM_MEMORY_CANARY" not in json.dumps(packet)
            assert METRICS.decode() not in [message.get("content") for message in packet["messages"]]
            delta = tool_call("read_work_memory", {"claim_id": "claim-one"}, "memory-first")
        elif read_result:
            assert results[-1]["tool_call_id"] == ("memory-first" if phase == 0 else "memory-again")
            value = json.loads(text_result(results[-1]))
            assert value["schema_version"] == "fa-work-memory-read-v1"
            assert value["source"] == metadata["source"] and value["text"] == NOTE
            assert [original["text"].encode("utf-8") for original in value["originals"]] == [METRICS]
            assert value["source"]["text_sha256"] == hashlib.sha256(NOTE.encode()).hexdigest()
            assert "reviewer_id" not in value and "reviewed_checks" not in value
            delta = (tool_call("execute_operation", {}, "memory-no-approval") if phase == 0 else
                     {"content": "Offline original reviewed note read again; no memory update or operation performed."})
        elif phase == 0:
            assert number == 3 and results[-1]["tool_call_id"] == "memory-no-approval"
            assert "Authority refused" in text_result(results[-1])
            delta = {"content": "Offline reviewed note and exact source were read; operation remains unapproved."}
        elif number == 1:
            previous = next(value for value in results if value["tool_call_id"] == "memory-first")
            assert json.loads(text_result(previous))["text"] == NOTE
            delta = tool_call("read_work_memory", {"claim_id": "not-assigned"}, "memory-unavailable")
        else:
            assert number == 2 and results[-1]["tool_call_id"] == "memory-unavailable"
            assert "Authority refused" in text_result(results[-1]) and "TEAM_MEMORY_CANARY" not in text_result(results[-1])
            delta = tool_call("read_work_memory", {"claim_id": "claim-one"}, "memory-again")
        calls.append({"turn": phase, "request": number, "exact_tool_result": read_result,
                      "prefix_sha256": PREFIX_SHA, "source_sha256": hashlib.sha256(
                          json.dumps(metadata["source"], sort_keys=True).encode()).hexdigest()})
        return sse(request, delta, len(calls))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_REVIEWED_NOTE")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    conversation, sdk_id, retained, references = None, None, {}, []
    with TemporaryDirectory(prefix="wm-sdk-") as directory:
        for phase in range(2):
            h.executor.root = Path(directory) / str(phase)
            accepted, _ = submit(h, f"memory-native-{phase}", texts[phase], conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            op_key = control_key("operation", binding.operation_id)
            operation = h.store.read_snapshot((op_key,))[op_key]
            done = h.runtime.controller.step(binding.binding_id)
            assert done.state == "succeeded" and done.result_forwarded, calls
            assert h.store.read_snapshot((op_key,))[op_key] == operation
            assert h.store.read_snapshot(tuple(retained)) == retained
            registration = control.records[binding.binding_id]
            retained.update(h.store.read_snapshot((key("text-archive", binding.job_id),
                work_policy_key("registration", registration.admission_id))))
            archived = saved_turn(h, binding.job_id)
            references.append(archived.work_memory_source)
            assert archived.prompt_assembly == prompt.record
            bridge, options = bridges[-1], configurations[-1]
            sdk_id = sdk_id or bridge.client.assignment.sdk_session_id
            assert bridge.client.assignment.sdk_session_id == sdk_id
            assert options["enable_config_discovery"] is False and options["mcp_servers"] == {}
            assert all(options[field] == [] for field in (
                "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
            if phase:
                assert options["continue_pending_work"] is False
            events = events_from(bridge._session_files.snapshot().files)
            assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == list(texts[:phase+1])
            assert not any(event["type"] in {"skill.invoked", "session.compaction_start"} for event in events)
            assert bridge._sdk_stop.graceful
    assert references[0] == references[1] and user_sends == list(texts)
    assert not forced and len(calls) == 6 and importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    response = h.client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
    assert response.status_code == 200
    projection = ConversationProjection.model_validate_json(response.content)
    assert projection.can_follow_up and all(value.prompt_assembly == prompt.record for value in projection.turns)
    for turn in projection.turns:
        public = turn.progress
        starts = {event.seq: event for event in public.events if event.kind == "tool_started"}
        memory_results = [event for event in public.events
                          if event.kind == "tool_completed" and event.tool_name == "read_work_memory"]
        assert any(event.status == "succeeded" for event in memory_results)
        assert all(event.source is None and event.tool_start_seq in starts for event in memory_results)
        assert all(starts[event.tool_start_seq].tool_name == "read_work_memory" for event in memory_results)
        assert "TEAM_MEMORY_CANARY" not in public.model_dump_json()
        assert "memory-first" not in public.model_dump_json() and "memory-again" not in public.model_dump_json()
    if os.environ.get("FA_WORK_MEMORY_EVIDENCE_DIR"):
        target = Path(os.environ["FA_WORK_MEMORY_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "reviewed-work-memory-native.json").write_text(json.dumps({
            "profile": PROFILE, "sdk_version": "1.0.13", "runtime_version": "1.0.83",
            "prefix_sha256": PREFIX_SHA, "prefix_utf8_bytes": 2719,
            "claim_sha256": references[0].claim_sha256, "claim_text_sha256": references[0].text_sha256,
            "review_sha256": references[0].review_sha256, "source_hashes": [value.sha256 for value in references[0].sources],
            "calls": calls, "offline_requests": 6,
            "stops": [value._sdk_stop.model_dump(mode="json") for value in bridges],
            "force_stop_calls": 0, "same_sdk_and_conversation": True, "original_archive_registration_retained": True,
            "initial_claim_text_absent": True, "exact_read_tool_and_source_in_next_request": True,
            "unavailable_alias_failed_without_data": True, "operation_remained_unapproved": True,
            "raw_sdk_user_and_events_exact": True, "strict_public_projection_accepted": True,
            "native_memory_calls": 0, "cloud_calls": 0, "paid_model_requests": 0,
            "private_agent_or_human_preference_memory": "NOT_SUPPORTED",
            "M_quality_and_full_PA13_PA16": "NOT_RUN",
        }, indent=2) + "\n", encoding="utf-8")
