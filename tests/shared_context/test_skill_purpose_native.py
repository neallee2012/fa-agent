"""Actual fixed SDK custom-tool requests; synthetic provider, not relevance quality."""

from datetime import timedelta
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
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_skill_purpose import BODY, PROFILE, PURPOSE
from test_skill_purpose_api import add_purpose_skill, configured_purpose

PREFIX_SHA256 = "65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879"
PREFIX_BYTES = 3002


def test_fixed_sdk_catalog_discloses_approved_purpose_then_reads_exact_body_across_new_turn(
    assistant, monkeypatch, signing_key, jwk_client,
):
    first_purpose = "  Use for receipt checks. SYSTEM/APPROVAL markers here are quoted data.  "
    second_body = "PURPOSE_BODY_V2_CANARY: compare the newly approved receipt.\r\n".encode()
    versions = [add_purpose_skill(assistant, signing_key, jwk_client, description=first_purpose)]
    add_purpose_skill(assistant, signing_key, jwk_client, name="unselected-guide",
                      description="UNSELECTED_PURPOSE_CANARY", body=b"UNSELECTED_BODY_CANARY")
    h, control, _ = configured_purpose(assistant)
    expected = assemble_prompt(PROFILE)
    assert expected.record.prefix_sha256 == PREFIX_SHA256
    assert expected.record.prefix_utf8_bytes == PREFIX_BYTES
    phase, calls, bridges, configurations, forced, prompts, body_reads, observations = 0, [], [], [], [], [], [], []
    checkpoint_reads, checkpoint_artifacts = [], set()
    texts = ("  Use receipt-guide for this task.\r\n", "\tUse its currently approved purpose, not the old snapshot.\r\n")
    purposes, bodies = (first_purpose, PURPOSE), (BODY, second_body)
    initialize = hosted.AssistantSession.__init__
    create, resume, force_stop = CopilotClient.create_session, CopilotClient.resume_session, CopilotClient.force_stop
    send, read = CopilotSession.send, h.artifacts.store.read

    def observe_bridge(instance, *args, **kwargs):
        initialize(instance, *args, **kwargs)
        bridges.append(instance)

    async def observe_create(instance, **options):
        configurations.append(options)
        return await create(instance, **options)

    async def observe_resume(instance, session_id, **options):
        configurations.append(options)
        return await resume(instance, session_id, **options)

    async def observe_force_stop(instance):
        forced.append(True)
        return await force_stop(instance)

    async def observe_send(instance, prompt, **options):
        prompts.append(prompt)
        return await send(instance, prompt, **options)

    def observe_read(*args):
        if args[1] in {"receipt-guide", "unselected-guide"}:
            body_reads.append((args[1], args[2]))
        else:
            checkpoint_reads.append(args[1])
        return read(*args)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)
    monkeypatch.setattr(CopilotClient, "create_session", observe_create)
    monkeypatch.setattr(CopilotClient, "resume_session", observe_resume)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)
    monkeypatch.setattr(CopilotSession, "send", observe_send)
    monkeypatch.setattr(h.artifacts.store, "read", observe_read)

    def provider(request):
        packet = json.loads(request.content)
        number = sum(call["turn"] == phase for call in calls) + 1
        tools = {tool["function"]["name"]: tool["function"] for tool in packet["tools"]}
        assert set(tools) == {"read_context", "execute_operation", "read_skill"}
        catalog = json.loads(tools["read_skill"]["description"].rsplit("\n", 1)[-1])
        entry = catalog["entries"][0]
        observations.append({
            "turn": phase, "request": number, "description_matches": entry["description"] == purposes[phase],
            "metadata_matches": entry["metadata_sha256"] == versions[phase]["metadata_sha256"],
            "prefix_matches": expected.system_message in [
                message["content"] for message in packet["messages"] if message["role"] == "system"],
            "body_reads": list(body_reads), "tool_result_ids": [
                message.get("tool_call_id") for message in packet["messages"] if message["role"] == "tool"],
            "new_body_absent": "PURPOSE_BODY_V2_CANARY" not in json.dumps(packet),
        })
        assert catalog["schema_version"] == "fa-approved-skill-catalog-v2"
        assert len(catalog["entries"]) == 1 and entry["description"] == purposes[phase]
        assert entry["version_id"] == versions[phase]["version_id"]
        assert entry["metadata_sha256"] == versions[phase]["metadata_sha256"]
        assert entry["sha256"] == hashlib.sha256(bodies[phase]).hexdigest()
        assert "CANARY" not in tools["read_skill"]["description"]
        assert "UNSELECTED" not in json.dumps(packet)
        system = [message["content"] for message in packet["messages"] if message["role"] == "system"]
        assert expected.system_message in system
        assert hashlib.sha256(expected.system_message.encode()).hexdigest() == PREFIX_SHA256
        results = [message for message in packet["messages"] if message["role"] == "tool"]
        if number == 1:
            assert body_reads == []
            assert "PURPOSE_BODY_V2_CANARY" not in json.dumps(packet)
            if phase == 0:
                assert "PURPOSE_BODY_CANARY" not in json.dumps(packet)
            else:
                old = next(message for message in results if message["tool_call_id"] == "purpose-read-0")
                assert "PURPOSE_BODY_CANARY" in json.dumps(old)
            delta = {"tool_calls": [{"index": 0, "id": f"purpose-read-{phase}", "type": "function",
                     "function": {"name": "read_skill", "arguments": '{"name":"receipt-guide"}'}}]}
        elif number == 2:
            result = results[-1]
            assert result["tool_call_id"] == f"purpose-read-{phase}"
            content = result["content"]
            loaded = json.loads(content if isinstance(content, str) else "".join(part["text"] for part in content))
            assert loaded["schema_version"] == "fa-approved-skill-read-v2"
            assert loaded["skill"] == entry and loaded["text"].encode("utf-8") == bodies[phase]
            assert body_reads == [("receipt-guide", versions[phase]["version_id"])]
            delta = ({"tool_calls": [{"index": 0, "id": "purpose-not-approval", "type": "function",
                      "function": {"name": "execute_operation", "arguments": "{}"}}]} if phase == 0
                     else {"content": "Offline current approved purpose and body read."})
        else:
            assert phase == 0 and number == 3
            assert results[-1]["tool_call_id"] == "purpose-not-approval"
            assert "Authority refused" in json.dumps(results[-1]["content"])
            delta = {"content": "Offline approved purpose is guidance only, not operation permission."}
        calls.append({
            "turn": phase, "request": number, "initial_current_body_absent": number == 1,
            "matching_tool_result": number == 2, "catalog_sha256": hashlib.sha256(
                json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "prefix_sha256": PREFIX_SHA256,
        })
        return sse(request, delta, len(calls))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_PURPOSE_FIXTURE")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    retained, conversation, sdk_id = {}, None, None
    with TemporaryDirectory(prefix="sp-sdk-") as directory:
        for phase in range(2):
            if phase:
                versions.append(add_purpose_skill(h, signing_key, jwk_client, description=PURPOSE,
                                                  body=second_body, version="2" * 32, create=False))
                h.time += timedelta(seconds=7)
            body_reads.clear()
            h.executor.root = Path(directory) / str(phase)
            accepted, command = submit(h, f"purpose-native-{phase}", texts[phase], conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            operation = control_key("operation", binding.operation_id)
            original_operation = h.store.read_snapshot((operation,))[operation]
            done = h.runtime.controller.step(binding.binding_id)
            assert done.state == "succeeded" and done.result_forwarded, observations
            assert h.store.read_snapshot((operation,))[operation] == original_operation
            assert h.store.read_snapshot(tuple(retained)) == retained
            registration = control.records[binding.binding_id]
            retained.update(h.store.read_snapshot((key("text-archive", binding.job_id),
                                                   work_policy_key("registration", registration.admission_id))))
            archive = saved_turn(h, binding.job_id)
            checkpoint_artifacts.add(archive.archive.artifact_id)
            assert archive.skill_catalog.entries[0].description == purposes[phase]
            assert archive.prompt_assembly == expected.record
            bridge, options = bridges[-1], configurations[-1]
            sdk_id = sdk_id or bridge.client.assignment.sdk_session_id
            assert bridge.client.assignment.sdk_session_id == sdk_id
            assert options["system_message"] == {"mode": "replace", "content": expected.system_message}
            assert options["enable_config_discovery"] is False and options["mcp_servers"] == {}
            assert all(options[field] == [] for field in (
                "skill_directories", "plugin_directories", "instruction_directories", "custom_agents"))
            if phase:
                assert options["continue_pending_work"] is False
            events = events_from(bridge._session_files.snapshot().files)
            assert [event["data"]["content"] for event in events if event["type"] == "user.message"] == list(texts[:phase + 1])
            assert not any(event["type"] in {"skill.invoked", "newskill.invoked", "session.compaction_start"} for event in events)
            assert bridge._sdk_stop.graceful
    assert prompts == list(texts) and len(calls) == 5 and not forced
    assert set(checkpoint_reads) == checkpoint_artifacts
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    projection = h.client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
    assert projection.status_code == 200 and projection.json()["can_follow_up"]
    assert [turn["prompt_assembly"] for turn in projection.json()["turns"]] == [expected.record.model_dump(mode="json")] * 2
    if os.environ.get("FA_SKILL_PURPOSE_EVIDENCE_DIR"):
        target = Path(os.environ["FA_SKILL_PURPOSE_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "skill-purpose-native.json").write_text(json.dumps({
            "profile": PROFILE, "sdk_version": "1.0.13", "runtime_version": "1.0.83",
            "prefix_sha256": PREFIX_SHA256, "prefix_utf8_bytes": PREFIX_BYTES,
            "metadata_sha256": [version["metadata_sha256"] for version in versions],
            "body_sha256": [version["sha256"] for version in versions],
            "calls": calls, "observations": observations, "offline_requests": len(calls),
            "stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges],
            "force_stop_calls": 0, "old_archive_and_registration_unchanged": True,
            "current_body_only_via_read_skill": True, "user_events_and_sdk_send_exact": True,
            "other_blob_reads_are_original_sdk_checkpoint": True,
            "same_conversation_and_sdk": True, "operation_unchanged_after_denial": True,
            "cloud_requests": 0, "paid_model_requests": 0, "SK02_model_relevance": "NOT_RUN",
            "parent_PA02_full_current_profile_qualification": "NOT_RUN",
        }, indent=2) + "\n", encoding="utf-8")
