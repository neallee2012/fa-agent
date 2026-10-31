"""Fixed SDK compaction and exact-original custom tool; scripted offline transport."""

import hashlib
import importlib.metadata
import json
import os
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace

import httpx
from copilot import CopilotClient
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_operations import control_key
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_sdk_state import CheckpointProfile, DIAG_ARCHIVE_CODEC
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_user_turn import UserTurnRead
from shared_context.behavior_user_turn_prompt import USER_TURN_CAPABILITIES, USER_TURN_PROFILE
from shared_context.behavior_work_policy_api import install_work_policy_routes
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import signing_key, jwk_client
from test_user_turn_read import ORIGINAL, configured


def test_compacted_new_runtime_reads_saved_user_original_not_summary(assistant, monkeypatch):
    assistant.runtime.profiles = MappingProxyType({
        name: profile.model_copy(update={"checkpoint_profile": CheckpointProfile()})
        for name, profile in assistant.runtime.profiles.items()
    })
    h, control, _ = configured(assistant)
    prefix = assemble_prompt(USER_TURN_PROFILE)
    summary = ("<overview>A synthetic conversation completed its first request.</overview>"
               "<history>The exact identifier and original user wording were intentionally omitted. "
               "Use an authorized source if a later request needs exact wording.</history>"
               "<checkpoint_title>Original omitted</checkpoint_title>")
    first_reply = "Synthetic saved answer.\n" + "\n".join(
        f"Public filler {number}: {hashlib.sha256(str(number).encode()).hexdigest()}" for number in range(350))
    texts = (ORIGINAL, "Acknowledge that the original task is complete.",
             "Quote my exact first user message, including its identifier and whitespace. Use read_user_turn for turn 1.",
             "A different conversation and task. Reply without reading another conversation.")
    phase, calls, bridges, options, force_stops, reads = 0, [], [], [], [], []
    initialize = hosted.AssistantSession.__init__
    create, resume, force_stop = CopilotClient.create_session, CopilotClient.resume_session, CopilotClient.force_stop
    read = h.assistant.read_user_turn

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def observe_create(instance, **configuration):
        options.append(configuration)
        return await create(instance, **configuration)

    async def observe_resume(instance, session_id, **configuration):
        options.append(configuration)
        return await resume(instance, session_id, **configuration)

    async def observe_force_stop(instance):
        force_stops.append(True)
        return await force_stop(instance)

    def observe_read(session, command, principal):
        cost = h.store.read_snapshot((control.cost_key,))[control.cost_key]
        result = read(session, command, principal)
        assert h.store.read_snapshot((control.cost_key,))[control.cost_key] == cost
        reads.append(result)
        return result

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    monkeypatch.setattr(CopilotClient, "create_session", observe_create)
    monkeypatch.setattr(CopilotClient, "resume_session", observe_resume)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)
    monkeypatch.setattr(h.assistant, "read_user_turn", observe_read)

    def provider(request):
        body = json.loads(request.content)
        number = sum(value["phase"] == phase for value in calls) + 1
        compacting = bridges[-1]._session_files.compacting
        messages = body["messages"]
        observation = {"phase": phase, "request": number, "checkpoint_compaction": compacting,
            "original_identifier_present": "8ef55d7a9c3b" in json.dumps(messages),
            "summary_present": "<overview>" in json.dumps(messages),
            "prefix_exact": prefix.system_message in [message["content"] for message in messages if message["role"] == "system"]}
        calls.append(observation)
        if compacting:
            assert phase == 1 and number == 1
            assert "8ef55d7a9c3b" in json.dumps(messages)
            delta = {"content": summary}
            check = {"summary_input_has_original": True}
        else:
            assert prefix.system_message in [message["content"] for message in messages if message["role"] == "system"]
            tools = {value["function"]["name"]: value["function"] for value in body["tools"]}
            assert set(tools) == set(USER_TURN_CAPABILITIES)
            assert set(tools["read_user_turn"]["parameters"]["properties"]) == {"turn_number"}
            if phase == 0:
                assert "fa-user-turn-read-v1" not in json.dumps(messages)
                delta = {"content": first_reply}
                check = {"initial_no_original_tool_result": True}
            elif phase == 1:
                assert "8ef55d7a9c3b" not in summary
                assert "<overview>" in json.dumps(messages)
                delta = {"content": "Offline compaction acknowledgement; exact original is not in the summary."}
                check = {"summary_omits_original": True}
            elif phase == 3:
                assert number == 1 and "8ef55d7a9c3b" not in json.dumps(messages)
                assert "fa-user-turn-read-v1" not in json.dumps(messages)
                delta = {"content": "Offline independent conversation; same fixed profile and no borrowed original."}
                check = {"different_conversation_original_absent": True}
            elif number == 1:
                assert "8ef55d7a9c3b" not in summary
                assert "<overview>" in json.dumps(messages)
                delta = {"tool_calls": [{"index": 0, "id": "read-original-user", "type": "function",
                    "function": {"name": "read_user_turn", "arguments": '{"turn_number":1}'}}]}
                check = {"fresh_runtime_summary_omits_original": True,
                         "full_model_eviction_proven": not observation["original_identifier_present"]}
            else:
                assert phase == 2 and number == 2 and len(reads) == 1
                result = [message for message in messages if message["role"] == "tool"][-1]
                assert result["tool_call_id"] == "read-original-user"
                content = result["content"]
                raw = content if isinstance(content, str) else "".join(part["text"] for part in content)
                original = UserTurnRead.model_validate_json(raw)
                assert original == reads[0] and original.text == ORIGINAL
                assert original.source.request_id == "original-first" and original.source.turn_number == 1
                assert original.source.sha256 == hashlib.sha256(ORIGINAL.encode("utf-8")).hexdigest()
                assert original.source.role == "user" and original.usage == "historical-data-not-current-instructions"
                delta = {"content": ORIGINAL}
                check = {"actual_api_original_in_next_model_request": True,
                         "tool_source_sha256": original.source.sha256}
        observation.update(check)
        return sse(request, delta, len(calls))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_ORIGINAL_READER_SECRET")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    h.executor.model_transport = None
    retained, conversation, archives = {}, None, []
    with TemporaryDirectory(prefix="user-original-") as directory:
        for phase in range(4):
            if phase:
                h.time += timedelta(seconds=10)
            h.executor.root = Path(directory) / str(phase)
            identifier = ("original-first", "original-compact", "original-reread", "original-independent")[phase]
            accepted, _ = submit(h, identifier, texts[phase], None if phase == 3 else conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            operation_key = control_key("operation", binding.operation_id)
            operation = h.store.read_snapshot((operation_key,))[operation_key]
            done = h.runtime.controller.step(binding.binding_id)
            assert done.state == "succeeded" and done.result_forwarded, calls
            assert h.store.read_snapshot((operation_key,))[operation_key] == operation
            assert h.store.read_snapshot(tuple(retained)) == retained
            archive = saved_turn(h, binding.job_id)
            archives.append(archive)
            assert archive.prompt_assembly == prefix.record
            if phase in {1, 2}:
                assert archive.archive.codec == DIAG_ARCHIVE_CODEC
            retained.update(h.store.read_snapshot((key("text-archive", binding.job_id),
                                                   key("result", binding.work.work_id))))
            events = events_from(bridges[-1]._session_files.snapshot().files)
            expected_users = [texts[3]] if phase == 3 else list(texts[:phase + 1])
            assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == expected_users
        projection = h.client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
        assert projection.status_code == 200 and projection.json()["can_follow_up"]
    assert len(calls) == 6 and len(reads) == 1 and len(bridges) == 4 and force_stops == []
    assert len({bridge._sdk_stop.pid for bridge in bridges}) == 4
    assert len({bridge.client.assignment.sdk_session_id for bridge in bridges[:3]}) == 1
    assert bridges[3].client.assignment.sdk_session_id != bridges[0].client.assignment.sdk_session_id
    assert all(bridge._sdk_stop.graceful for bridge in bridges)
    assert archives[2].user_turn_sources == (reads[0].source,)
    assert archives[1].archive.source.execution == archives[2].archive.source.execution
    for configuration in options:
        assert configuration["enable_config_discovery"] is False and configuration["mcp_servers"] == {}
        assert all(configuration[name] == [] for name in (
            "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
    if os.environ.get("FA_USER_TURN_EVIDENCE_DIR"):
        destination = Path(os.environ["FA_USER_TURN_EVIDENCE_DIR"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "user-original-native.json").write_text(json.dumps({
            "scope": "fixed SDK compaction/restore/custom tool plus real signed local API; scripted offline provider",
            "sdk_version": importlib.metadata.version("github-copilot-sdk"), "runtime_version": "1.0.83",
            "requests": calls, "graceful_stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges],
            "force_stop_calls": 0, "prefix_sha256": prefix.record.prefix_sha256,
            "prefix_utf8_bytes": prefix.record.prefix_utf8_bytes, "original_sha256": reads[0].source.sha256,
            "original_utf8_bytes": reads[0].source.utf8_bytes,
            "original_archive_and_results_retained": True, "sdk_user_events_exact": True,
            "reader_did_not_change_cost_ledger": True, "unapproved_operations_unchanged": True,
            "new_profile_prefix_stable_across_work_time_user_and_conversation": True,
            "native_builtin_or_discovery_enabled": False, "cloud_requests": 0, "paid_model_requests": 0,
            "full_model_eviction_proven": False,
            "limitation": "The summary omits the identifier, but the pinned runtime retains it in model-facing user history.",
            "semantic_quality": "NOT_RUN", "full_TC03_TC06_TC07": "NOT_RUN",
        }, indent=2) + "\n", encoding="utf-8")
