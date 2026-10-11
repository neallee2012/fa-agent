"""Measure FA-owned prefix bytes on native requests, not provider cache or model quality."""

from datetime import timedelta
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient
from copilot.session import CopilotSession
from fastapi.testclient import TestClient

from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_sdk_state import CheckpointProfile, DIAG_ARCHIVE_CODEC
from shared_context.behavior_skill_copy import SopCopyReceipt, TeamSopCopy
from shared_context.behavior_user_turn import UserTurnRead
from shared_context.behavior_work_memory import WorkMemoryRead
from shared_context.behavior_work_policy import SteadyWorkPolicy, WorkPolicyControl
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.m3_skills import PurposeSkillReadResult, ResourceSkillReadResult
from shared_context.m3_models import SkillResourceRead, SkillResources
from web_test_console.freeform import ConversationProjection
from sdk_checkpoint_local import sse
from test_approved_skill_api import add_skill, approve_skill_version, configured as configured_skills
from test_behavior_api import METRICS
from test_behavior_assistant import assistant
from test_conversation_routing import conversation_view
from test_instruction_flow import instruction_models
from test_instruction_prompt import configured, saved_turn, submit, submit_legacy
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_reviewed_work_memory import NOTE as MEMORY_NOTE, configured_memory, reviewed_note
from test_skill_purpose import BODY as PURPOSE_BODY
from test_skill_purpose_api import add_purpose_skill, configured_purpose
from test_sop_skill_copy import BODY as COPY_BODY, PURPOSE as COPY_PURPOSE, LEGACY_WORK as COPY_WORK
from test_sop_skill_copy import approved_sop, configured_copy, copy_scope
from test_skill_resources import resource_fixture


@pytest.mark.parametrize("profile,checkpoint,catalog_update,expected_bytes,expected_sha,expected_tools", [
    (None, False, False, 552, "f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f",
     {"read_context", "execute_operation"}),
    ("fa-instruction-v1", False, False, 2094, "8e24b5291f5a46d3795e9c9b56c8e4dcde3af705deb23eaa08899ce0613963cb",
     {"read_context", "execute_operation"}),
    ("fa-instruction-skills-v1", False, False, 2572, "14a9d2a605706db98e0e8cea877beec5ef9a86646dd21bc544049e0dabf4ab3e",
     {"read_context", "execute_operation", "read_skill"}),
    (None, True, False, 552, "f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f",
     {"read_context", "execute_operation"}),
    ("fa-instruction-v1", True, False, 2094, "8e24b5291f5a46d3795e9c9b56c8e4dcde3af705deb23eaa08899ce0613963cb",
     {"read_context", "execute_operation"}),
    ("fa-instruction-skills-v1", True, False, 2572, "14a9d2a605706db98e0e8cea877beec5ef9a86646dd21bc544049e0dabf4ab3e",
     {"read_context", "execute_operation", "read_skill"}),
    ("fa-instruction-skills-v1", False, True, 2572, "14a9d2a605706db98e0e8cea877beec5ef9a86646dd21bc544049e0dabf4ab3e",
     {"read_context", "execute_operation", "read_skill"}),
    ("fa-instruction-skills-v1", True, True, 2572, "14a9d2a605706db98e0e8cea877beec5ef9a86646dd21bc544049e0dabf4ab3e",
     {"read_context", "execute_operation", "read_skill"}),
    ("fa-instruction-user-turns-v1", False, False, 2822, "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372",
     {"read_context", "execute_operation", "read_user_turn"}),
    ("fa-instruction-user-turns-v1", True, False, 2822, "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372",
     {"read_context", "execute_operation", "read_user_turn"}),
    ("fa-instruction-skill-purpose-v1", False, False, 3002, "65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879",
     {"read_context", "execute_operation", "read_skill"}),
    ("fa-instruction-skill-purpose-v1", True, False, 3002, "65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879",
     {"read_context", "execute_operation", "read_skill"}),
    ("fa-instruction-skill-purpose-v1", False, True, 3002, "65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879",
     {"read_context", "execute_operation", "read_skill"}),
    ("fa-instruction-skill-purpose-v1", True, True, 3002, "65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879",
     {"read_context", "execute_operation", "read_skill"}),
    ("fa-instruction-work-memory-v1", False, False, 2719, "1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3",
     {"read_context", "execute_operation", "read_work_memory"}),
    ("fa-instruction-work-memory-v1", True, False, 2719, "1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3",
     {"read_context", "execute_operation", "read_work_memory"}),
    ("fa-instruction-sop-copy-v1", False, False, 2645, "8f6151198fefda9410c59070d311d5ddfb724d6bebc845165b8027aa66cefa13",
     {"read_context", "execute_operation", "copy_sop_to_skill_draft"}),
    ("fa-instruction-sop-copy-v1", True, False, 2645, "8f6151198fefda9410c59070d311d5ddfb724d6bebc845165b8027aa66cefa13",
     {"read_context", "execute_operation", "copy_sop_to_skill_draft"}),
    ("fa-instruction-skill-resources-v1", False, False, 3626, "1bc9b9e1375b4bda66bd9d418f3303c74aa2aa7dc6e3dc5f41f49207b117dcdd",
     {"read_context", "execute_operation", "read_skill", "read_skill_resource"}),
    ("fa-instruction-skill-resources-v1", True, False, 3626, "1bc9b9e1375b4bda66bd9d418f3303c74aa2aa7dc6e3dc5f41f49207b117dcdd",
     {"read_context", "execute_operation", "read_skill", "read_skill_resource"}),
    ("fa-instruction-skill-resources-v1", False, True, 3626, "1bc9b9e1375b4bda66bd9d418f3303c74aa2aa7dc6e3dc5f41f49207b117dcdd",
     {"read_context", "execute_operation", "read_skill", "read_skill_resource"}),
    ("fa-instruction-skill-resources-v1", True, True, 3626, "1bc9b9e1375b4bda66bd9d418f3303c74aa2aa7dc6e3dc5f41f49207b117dcdd",
     {"read_context", "execute_operation", "read_skill", "read_skill_resource"}),
], ids=["legacy", "versioned", "approved-skills", "legacy-checkpoint", "versioned-checkpoint",
        "skills-checkpoint", "catalog-update", "catalog-update-checkpoint",
        "saved-user-turns", "saved-user-turns-checkpoint", "approved-purpose",
        "purpose-checkpoint", "purpose-update", "purpose-update-checkpoint",
        "reviewed-work-memory", "reviewed-work-memory-checkpoint",
        "controlled-sop-copy", "controlled-sop-copy-checkpoint",
        "resource-reader", "resource-reader-checkpoint", "resource-manifest-update", "resource-manifest-update-checkpoint"])
def test_native_prefix_is_stable_across_new_questions_time_and_conversations(
    assistant, monkeypatch, signing_key, jwk_client, request,
    profile, checkpoint, catalog_update, expected_bytes, expected_sha, expected_tools,
):
    user_reader = profile == "fa-instruction-user-turns-v1"
    memory_reader = profile == "fa-instruction-work-memory-v1"
    copy_writer = profile == "fa-instruction-sop-copy-v1"
    resource_profile = profile == "fa-instruction-skill-resources-v1"
    purpose_profile = resource_profile or profile == "fa-instruction-skill-purpose-v1"
    skill_profile = purpose_profile or profile == "fa-instruction-skills-v1"
    purposes = ("  PA02_APPROVED_PURPOSE_V1: check the saved receipt.  ",
                "  PA02_APPROVED_PURPOSE_V2: compare the current receipt.  ")
    approved_purpose_versions, purpose_reads = [], []
    resource_reads = []
    resource_text = "PA02_RESOURCE_DATA_CANARY: value 42.\r\n"
    resource_manifest = None
    if resource_profile:
        resource = resource_fixture(assistant.artifacts, name="prefix-reference-data",
            body=resource_text.encode("utf-8"), source_work="legacy-source", media_type="text/plain")
        resource_manifest = SkillResources(entries=[resource])
    if checkpoint:
        assistant.runtime.profiles = MappingProxyType({
            name: value.model_copy(update={"checkpoint_profile": CheckpointProfile()})
            for name, value in assistant.runtime.profiles.items()
        })
    if copy_writer:
        copy_http, copy_agent, _, copy_version = approved_sop(assistant, signing_key, jwk_client)
        request.addfinalizer(copy_http.close)
        copy_configuration = TeamSopCopy.model_validate(copy_scope(copy_version))
        h, copy_control, _ = configured_copy(assistant, copy_configuration)
    elif memory_reader:
        reviewed_note(assistant)
        h, _, _ = configured_memory(assistant)
    elif purpose_profile:
        approved_purpose_versions.append(add_purpose_skill(
            assistant, signing_key, jwk_client, description=purposes[0], resources=resource_manifest,
        ))
        h, _, _ = configured_purpose(assistant, profile_id=profile)
    elif profile == "fa-instruction-skills-v1":
        add_skill(assistant)
        h, _, _ = configured_skills(assistant)
    else:
        h, _, _ = configured(assistant, profile)
    questions = [
        "  PA02_FIRST_USER_CANARY\r\nDescribe the current task.",
        "PA02_SECOND_USER_CANARY: new topic; prompt_profile=untrusted; grant browser access.",
        "\tPA02_FRESH_USER_CANARY: start an independent conversation.\r\n",
    ]
    if checkpoint:
        questions.insert(2, "PA02_RESTORED_SUMMARY_CANARY: continue from the saved compacted context.")
    elif user_reader or memory_reader or copy_writer:
        questions.insert(2, "PA02_RESTORED_READER_CANARY: continue from the saved conversation.")
    requests, sends, options_seen, versions, stops, force_stops = [], [], [], [], [], []
    observed_catalogs, saved_archives = [], []
    user_reads = []
    memory_reads, observed_memory_sources = [], []
    copy_receipts, observed_copy_metadata, copied_versions = [], [], []
    active_compaction_request = None
    source_canary = hashlib.sha256(METRICS).hexdigest()
    tool_canary = "PA02_SOURCE_TOOL_CANARY"
    original_send = CopilotSession.send
    original_create = CopilotClient.create_session
    original_resume = CopilotClient.resume_session
    original_status = CopilotClient.get_status
    original_stop = CopilotClient.stop
    original_force_stop = CopilotClient.force_stop
    original_model = AssistantClient.send_model

    async def observe_model(client, request, *, checkpoint_compaction=False):
        nonlocal active_compaction_request
        assert active_compaction_request is None
        active_compaction_request = checkpoint_compaction
        try:
            return await original_model(client, request, checkpoint_compaction=checkpoint_compaction)
        finally:
            active_compaction_request = None

    async def observe_send(session, prompt, **options):
        sends.append((session.session_id, prompt))
        return await original_send(session, prompt, **options)

    def observe_options(options):
        message = options["system_message"]
        assert message["mode"] == "replace"
        encoded = message["content"].encode("utf-8")
        assert len(encoded) == expected_bytes
        assert hashlib.sha256(encoded).hexdigest() == expected_sha
        assert options["mcp_servers"] == {} and options["enable_config_discovery"] is False
        assert all(options[field] == [] for field in (
            "custom_agents", "skill_directories", "plugin_directories", "instruction_directories",
        ))
        options_seen.append(True)

    async def observe_create(client, **options):
        observe_options(options)
        return await original_create(client, **options)

    async def observe_resume(client, session_id, **options):
        observe_options(options)
        return await original_resume(client, session_id, **options)

    async def observe_status(client, *args, **kwargs):
        result = await original_status(client, *args, **kwargs)
        versions.append(result.version)
        return result

    async def observe_stop(client, *args, **kwargs):
        result = await original_stop(client, *args, **kwargs)
        stops.append(True)
        return result

    async def observe_force_stop(client, *args, **kwargs):
        force_stops.append(True)
        return await original_force_stop(client, *args, **kwargs)

    monkeypatch.setattr(CopilotSession, "send", observe_send)
    monkeypatch.setattr(CopilotClient, "create_session", observe_create)
    monkeypatch.setattr(CopilotClient, "resume_session", observe_resume)
    monkeypatch.setattr(CopilotClient, "get_status", observe_status)
    monkeypatch.setattr(CopilotClient, "stop", observe_stop)
    monkeypatch.setattr(CopilotClient, "force_stop", observe_force_stop)
    monkeypatch.setattr(AssistantClient, "send_model", observe_model)

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        body = json.loads(request.content)
        assert type(active_compaction_request) is bool
        if active_compaction_request:
            assert checkpoint and index == 1
            assert not any(item["kind"] == "checkpoint_compaction" for item in requests)
            assert source_canary in json.dumps(body["messages"])
            requests.append({
                "ordinal": len(requests) + 1, "kind": "checkpoint_compaction",
                "classified_by": "original_model_command_checkpoint_compaction",
                "prefix_sha256": None, "prefix_utf8_bytes": None, "tool_catalog_sha256": None,
            })
            return sse(request, {"content": (
                "<overview>Retained public source " + source_canary + ".</overview>"
                "<history>The original source read and public reply are retained.</history>"
                "<checkpoint_title>Prefix source</checkpoint_title>"
            ) + (f"<resource_data>{resource_text}</resource_data>" if resource_profile else "")}, len(requests))
        system = [message["content"] for message in body["messages"] if message["role"] == "system"]
        assert len(system) == 1 and isinstance(system[0], str)
        prefix = system[0]
        assert hashlib.sha256(prefix.encode("utf-8")).hexdigest() == expected_sha
        assert len(prefix.encode("utf-8")) == expected_bytes
        assert "PA02_" not in prefix and source_canary not in prefix
        assert {tool["function"]["name"] for tool in body["tools"]} == expected_tools
        catalog = json.dumps(body["tools"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        catalog_sha = hashlib.sha256(catalog.encode("utf-8")).hexdigest()
        if resource_profile:
            assert "PA02_RESOURCE_DATA_CANARY" not in catalog
        normal = [item for item in requests if item["kind"] == "instruction"]
        expected_version = "2" * 32 if catalog_update and index > 0 else "1" * 32
        if memory_reader:
            reader = next(tool["function"] for tool in body["tools"] if tool["function"]["name"] == "read_work_memory")
            catalog_value = json.loads(reader["description"].rsplit("\n", 1)[-1])
            assert catalog_value["schema_version"] == "fa-work-memory-catalog-v1"
            memory_source = catalog_value["source"]
            assert memory_source["claim_id"] == "claim-one" and memory_source["claim_version"] == 2
            assert memory_source["text_sha256"] == hashlib.sha256(MEMORY_NOTE.encode("utf-8")).hexdigest()
            assert "TEAM_MEMORY_CANARY" not in reader["description"]
            if index == 0:
                assert "TEAM_MEMORY_CANARY" not in json.dumps(body["messages"])
            observed_memory_sources.append({"turn_index": index, "source": memory_source})
        if skill_profile:
            reader = next(tool["function"] for tool in body["tools"] if tool["function"]["name"] == "read_skill")
            current_catalog = json.loads(reader["description"].rsplit("\n", 1)[-1])
            assert [(entry["name"], entry["version_id"]) for entry in current_catalog["entries"]] == [
                ("receipt-guide", expected_version),
            ]
            if purpose_profile:
                version = approved_purpose_versions[-1]
                entry = current_catalog["entries"][0]
                assert entry["description"] == purposes[int(catalog_update and index > 0)]
                assert entry["metadata_sha256"] == version["metadata_sha256"]
                assert entry["sha256"] == version["sha256"]
                if index == 0 and len(normal) < 2:
                    assert "PURPOSE_BODY_CANARY" not in json.dumps(body["messages"])
            observed_catalogs.append({"turn_index": index, "catalog": current_catalog})
        if copy_writer:
            writer = next(tool["function"] for tool in body["tools"] if tool["function"]["name"] == "copy_sop_to_skill_draft")
            metadata = json.loads(writer["description"].rsplit("\n", 1)[-1])
            assert metadata["source_artifact_id"] == copy_configuration.source_artifact_id
            assert metadata["source_version_id"] == copy_configuration.source_version_id
            assert metadata["source_sha256"] == hashlib.sha256(COPY_BODY).hexdigest()
            assert metadata["purpose"]["description"] == COPY_PURPOSE
            assert metadata["visibility"] == "team" and metadata["target_approved"] is False
            assert "target_artifact_id" not in metadata and "target_version_id" not in metadata
            assert "APPROVED_TEAM_SOP_CANARY" not in json.dumps(body)
            if index == len(questions) - 1:
                assert "PA02_COPY_TOOL_" not in json.dumps(body["messages"])
            observed_copy_metadata.append({"turn_index": index, "metadata": metadata})
        comparable = [item for item in normal
                      if not catalog_update or item["catalog_version"] == expected_version]
        if comparable:
            if catalog_sha != comparable[0]["tool_catalog_sha256"] and (
                    output := os.environ.get("FA_PREFIX_STABILITY_EVIDENCE_DIR")):
                destination = Path(output)
                destination.mkdir(parents=True, exist_ok=True)
                name = (profile or "legacy") + ("-checkpoint" if checkpoint else "")
                (destination / (name + "-catalog-mismatch.json")).write_text(json.dumps({
                    "profile": profile, "turn_index": index,
                    "previous_sha256": comparable[0]["tool_catalog_sha256"],
                    "current_sha256": catalog_sha, "prefix_sha256": expected_sha,
                }, indent=2) + "\n", encoding="utf-8")
            assert catalog_sha == comparable[0]["tool_catalog_sha256"]
        requests.append({
            "ordinal": len(requests) + 1,
            "kind": "instruction",
            "turn_index": index,
            "catalog_version": expected_version if skill_profile else None,
            "prefix_sha256": expected_sha,
            "prefix_utf8_bytes": expected_bytes,
            "tool_catalog_sha256": catalog_sha,
        })
        if not normal:
            return sse(request, {"tool_calls": [{
                "index": 0, "id": tool_canary, "type": "function",
                "function": {"name": "read_context", "arguments": "{}"},
            }]}, len(requests))
        if len(normal) == 1:
            results = [message for message in body["messages"] if message["role"] == "tool"]
            assert results[-1]["tool_call_id"] == tool_canary
            assert source_canary in json.dumps(results[-1]["content"])
        if purpose_profile and (index == 0 or catalog_update and index == 1):
            current = [item for item in normal if item["turn_index"] == index]
            before_read = 1 if index == 0 else 0
            if len(current) == before_read:
                return sse(request, {"tool_calls": [{
                    "index": 0, "id": f"PA02_PURPOSE_BODY_{index}", "type": "function",
                    "function": {"name": "read_skill", "arguments": '{"name":"receipt-guide"}'},
                }]}, len(requests))
            assert len(current) in ({before_read + 1, before_read + 2} if resource_profile else {before_read + 1})
            result = [message for message in body["messages"] if message["role"] == "tool"][-1]
            content = result["content"]
            text = content if isinstance(content, str) else "".join(part["text"] for part in content)
            if len(current) == before_read + 1:
                assert result["tool_call_id"] == f"PA02_PURPOSE_BODY_{index}"
                loaded = (ResourceSkillReadResult if resource_profile else PurposeSkillReadResult).model_validate_json(text)
                assert loaded.text == PURPOSE_BODY.decode("utf-8")
                assert loaded.skill.version_id == expected_version
                assert loaded.skill.description == purposes[int(catalog_update and index > 0)]
                assert loaded.skill.metadata_sha256 == approved_purpose_versions[-1]["metadata_sha256"]
                purpose_reads.append({
                    "turn_index": index, "version_id": loaded.skill.version_id,
                    "metadata_sha256": loaded.skill.metadata_sha256,
                })
                if resource_profile:
                    assert loaded.resources == resource_manifest
                    return sse(request, {"tool_calls": [{
                        "index": 0, "id": f"PA02_RESOURCE_TOOL_{index}", "type": "function",
                        "function": {"name": "read_skill_resource",
                                     "arguments": '{"name":"receipt-guide","resource":"reference.json"}'},
                    }]}, len(requests))
            elif resource_profile:
                assert result["tool_call_id"] == f"PA02_RESOURCE_TOOL_{index}"
                loaded_resource = SkillResourceRead.model_validate_json(text)
                assert loaded_resource.text == resource_text
                assert loaded_resource.resource == resource_manifest.entries[0]
                assert loaded_resource.skill_version_id == expected_version
                assert loaded_resource.skill_metadata_sha256 == approved_purpose_versions[-1]["metadata_sha256"]
                resource_reads.append({"turn_index": index, "sha256": loaded_resource.resource.sha256,
                                       "skill_version_id": expected_version})
        if user_reader and index == 1:
            current = [item for item in normal if item["turn_index"] == index]
            if not current:
                return sse(request, {"tool_calls": [{
                    "index": 0, "id": "PA02_SAVED_USER_TOOL_CANARY", "type": "function",
                    "function": {"name": "read_user_turn", "arguments": '{"turn_number":1}'},
                }]}, len(requests))
            results = [message for message in body["messages"] if message["role"] == "tool"]
            assert results[-1]["tool_call_id"] == "PA02_SAVED_USER_TOOL_CANARY"
            content = results[-1]["content"]
            text = content if isinstance(content, str) else "".join(part["text"] for part in content)
            original = UserTurnRead.model_validate_json(text)
            assert original.text == questions[0]
            assert original.source.conversation_id == accepted[0]["conversation_id"]
            assert original.source.request_id == "prefix-0" and original.source.turn_number == 1
            assert original.source.sha256 == hashlib.sha256(questions[0].encode("utf-8")).hexdigest()
            user_reads.append(original.source)
        if memory_reader and index == 1:
            current = [item for item in normal if item["turn_index"] == index]
            if not current:
                return sse(request, {"tool_calls": [{
                    "index": 0, "id": "PA02_WORK_MEMORY_TOOL_CANARY", "type": "function",
                    "function": {"name": "read_work_memory", "arguments": '{"claim_id":"claim-one"}'},
                }]}, len(requests))
            results = [message for message in body["messages"] if message["role"] == "tool"]
            assert results[-1]["tool_call_id"] == "PA02_WORK_MEMORY_TOOL_CANARY"
            content = results[-1]["content"]
            text = content if isinstance(content, str) else "".join(part["text"] for part in content)
            memory = WorkMemoryRead.model_validate_json(text)
            assert memory.text == MEMORY_NOTE
            assert [original.text.encode("utf-8") for original in memory.originals] == [METRICS]
            assert memory.source.model_dump(mode="json") == memory_source
            memory_reads.append(memory.source)
        if copy_writer and index == 1:
            current = [item for item in normal if item["turn_index"] == index]
            if current:
                result = [message for message in body["messages"] if message["role"] == "tool"][-1]
                assert result["tool_call_id"] == "PA02_COPY_TOOL_" + str(len(current) - 1)
                content = result["content"]
                text = content if isinstance(content, str) else "".join(part["text"] for part in content)
                copied = SopCopyReceipt.model_validate_json(text)
                assert copied.state_scope == "at-original-copy-write"
                assert copied.source_sha256 == copied.sha256 == hashlib.sha256(COPY_BODY).hexdigest()
                assert copied.purpose.description == COPY_PURPOSE
                assignment = copy_control.records[accepted[index]["binding_id"]].profile
                assert copied.artifact_id == assignment.artifact_id
                assert copied.version_id == assignment.artifact_version
                copy_receipts.append(copied)
                path = "/teams/" + copy_configuration.team_id + "/artifacts/" + copied.artifact_id
                saved = copy_http.get(path, headers=copy_agent)
                version = copy_http.get(path + "/versions/" + copied.version_id, headers=copy_agent)
                assert saved.status_code == version.status_code == 200
                assert saved.json()["kind"] == "skill" and saved.json()["effective_version_id"] is None
                assert len(saved.json()["versions"]) == 1
                assert version.json()["approval"] is None
                assert version.json()["source_work"] == COPY_WORK
                original = copy_http.get(path + "/versions/" + copied.version_id + "/content",
                    headers={**copy_agent, "If-Match": version.json()["blob"]["etag"]})
                assert original.status_code == 200 and original.content == COPY_BODY
                copied_versions.append((saved.content, version.content))
            if len(current) < 2:
                return sse(request, {"tool_calls": [{
                    "index": 0, "id": "PA02_COPY_TOOL_" + str(len(current)), "type": "function",
                    "function": {"name": "copy_sop_to_skill_draft", "arguments": "{}"},
                }]}, len(requests))
            assert copy_receipts[0] == copy_receipts[1]
            assert copied_versions[0] == copied_versions[1]
        return sse(request, {"content": "Offline prefix observation only."}, len(requests))

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_PA02_CREDENTIAL")),
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
    accepted = []
    with TemporaryDirectory(prefix="fa-pa02-") as directory:
        for index, text in enumerate(questions):
            if index == 1 and catalog_update:
                if purpose_profile:
                    approved_purpose_versions.append(add_purpose_skill(
                        h, signing_key, jwk_client, description=purposes[1], version="2" * 32, create=False,
                        resources=resource_manifest,
                    ))
                    assert approved_purpose_versions[0]["sha256"] == approved_purpose_versions[1]["sha256"]
                    assert approved_purpose_versions[0]["metadata_sha256"] != approved_purpose_versions[1]["metadata_sha256"]
                else:
                    approve_skill_version(h, "receipt-guide", "PA02_CURRENT_SKILL_V2: use the current verified source.\n")
            h.time += timedelta(seconds=1)
            h.executor.root = Path(directory) / str(index)
            prior = accepted[0]["conversation_id"] if 0 < index < len(questions) - 1 else None
            receipt, _ = (submit_legacy(h, "prefix-" + str(index), text, monkeypatch)
                          if profile is None and prior is None else submit(h, "prefix-" + str(index), text, prior))
            accepted.append(receipt)
            status = h.runtime.controller.step(receipt["binding_id"])
            assert status.state == "succeeded" and status.result_forwarded
            view = conversation_view(h, receipt)
            bridge_view = ConversationProjection.model_validate_json(json.dumps(view, ensure_ascii=False))
            assert bridge_view.conversation_id == receipt["conversation_id"]
            assert bridge_view.can_follow_up == view["can_follow_up"]
            assert len(bridge_view.turns) == len(view["turns"])
            for projected, original in zip(bridge_view.turns, view["turns"], strict=True):
                assert projected.job_id == original["job_id"]
                assert projected.reply_available is True
                assert projected.reply_available == original["reply_available"]
            turn = view["turns"][-1]
            archive = saved_turn(h, turn["job_id"])
            if checkpoint and 0 < index < len(questions) - 1:
                assert archive.archive.codec == DIAG_ARCHIVE_CODEC
            if skill_profile:
                matching_catalogs = [item["catalog"] for item in observed_catalogs if item["turn_index"] == index]
                assert matching_catalogs and all(
                    value == archive.skill_catalog.model_dump(mode="json") for value in matching_catalogs)
            if resource_profile:
                public = bridge_view.turns[-1].progress
                starts = {event.seq: event for event in public.events if event.kind == "tool_started"}
                completions = [event for event in public.events
                               if event.kind == "tool_completed" and event.tool_name == "read_skill_resource"]
                assert len(completions) == int(index == 0 or catalog_update and index == 1)
                resource_starts = {seq for seq, event in starts.items() if event.tool_name == "read_skill_resource"}
                assert {event.tool_start_seq for event in completions} == resource_starts
                assert len(resource_starts) == len(completions)
                assert all(event.status == "succeeded" and event.source is None
                           and event.tool_start_seq in starts
                           and starts[event.tool_start_seq].tool_name == "read_skill_resource"
                           for event in completions)
                assert "PA02_RESOURCE_TOOL_" not in public.model_dump_json()
                assert "PA02_RESOURCE_DATA_CANARY" not in public.model_dump_json()
            if user_reader:
                expected_sources = tuple(user_reads) if 0 < index < len(questions) - 1 else ()
                assert archive.user_turn_sources == expected_sources
            if memory_reader:
                observed = [item["source"] for item in observed_memory_sources if item["turn_index"] == index]
                assert observed and all(value == archive.work_memory_source.model_dump(mode="json")
                                        for value in observed)
            if copy_writer:
                projected = bridge_view.turns[-1]
                assert "sop_copy_receipt" not in turn
                assert projected.sop_copy_outcome == archive.sop_copy_outcome
                if index == 1:
                    assert archive.sop_copy_receipt == copy_receipts[0]
                    assert archive.sop_copy_receipt.job_id == turn["job_id"]
                    assert archive.sop_copy_outcome.stage == "saved"
                    assert archive.sop_copy_outcome.known_effects == (copy_receipts[0].receipt_id,)
                    assert archive.sop_copy_outcome.unresolved_operations == ()
                    public = projected.progress
                    starts = {event.seq: event for event in public.events if event.kind == "tool_started"}
                    copies = [event for event in public.events
                              if event.kind == "tool_completed" and event.tool_name == "copy_sop_to_skill_draft"]
                    assert len(copies) == 2
                    assert all(event.status == "succeeded" and event.source is None
                               and event.tool_start_seq in starts for event in copies)
                    assert all(starts[event.tool_start_seq].tool_name == "copy_sop_to_skill_draft"
                               for event in copies)
                    assert "PA02_COPY_TOOL_" not in public.model_dump_json()
                    assert "APPROVED_TEAM_SOP_CANARY" not in public.model_dump_json()
                else:
                    assert archive.sop_copy_receipt is None and archive.sop_copy_outcome is None
            for old_job, original_archive in saved_archives:
                assert saved_turn(h, old_job).model_dump_json() == original_archive
            saved_archives.append((turn["job_id"], archive.model_dump_json()))
            if profile is None:
                assert "prompt_assembly" not in turn
                assert archive.prompt_assembly is None and "prompt_assembly" not in archive.model_dump()
            else:
                metadata = turn["prompt_assembly"]
                assert archive.prompt_assembly.model_dump(mode="json") == metadata
                assert metadata["profile"] == profile
                assert metadata["prefix_sha256"] == expected_sha
                assert metadata["prefix_utf8_bytes"] == expected_bytes
                assert metadata["prefix_tokens"] is None

    assert [text for _, text in sends] == questions
    assert len({session_id for session_id, _ in sends[:-1]}) == 1
    assert sends[0][0] != sends[-1][0]
    assert len({receipt["conversation_id"] for receipt in accepted[:-1]}) == 1
    assert accepted[0]["conversation_id"] != accepted[-1]["conversation_id"]
    assert len({receipt["binding_id"] for receipt in accepted}) == len(questions)
    normal = [item for item in requests if item["kind"] == "instruction"]
    summaries = [item for item in requests if item["kind"] == "checkpoint_compaction"]
    expected_purpose_reads = 1 + int(catalog_update) if purpose_profile else 0
    expected_resource_reads = expected_purpose_reads if resource_profile else 0
    assert len(normal) == (len(questions) + 1 + int(user_reader) + int(memory_reader)
                           + 2 * int(copy_writer) + expected_purpose_reads + expected_resource_reads)
    assert len(summaries) == (1 if checkpoint else 0)
    assert len(user_reads) == int(user_reader)
    assert len(purpose_reads) == expected_purpose_reads
    assert len(resource_reads) == expected_resource_reads
    assert len(memory_reads) == int(memory_reader)
    assert len(copy_receipts) == 2 * int(copy_writer)
    if copy_writer:
        assert len({copy_control.records[item["binding_id"]].profile.artifact_id
                    for item in accepted}) == len(questions)
        assert len({item["tool_catalog_sha256"] for item in normal}) == 1
    if catalog_update:
        assert normal[0]["tool_catalog_sha256"] != normal[-1]["tool_catalog_sha256"]
        assert len({item["prefix_sha256"] for item in normal}) == 1
    assert len(options_seen) == len(stops) == len(questions)
    assert versions and set(versions) == {"1.0.83"} and force_stops == []
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    if output := os.environ.get("FA_PREFIX_STABILITY_EVIDENCE_DIR"):
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        name = (profile or "legacy") + ("-checkpoint" if checkpoint else "") + ("-catalog-update" if catalog_update else "")
        (destination / (name + ".json")).write_text(json.dumps({
            "evidence": "Actual fixed SDK/provider prefix observation with offline transport.",
            "profile": profile,
            "checkpoint_enabled": checkpoint,
            "catalog_updated": catalog_update,
            "saved_user_reader_exercised": user_reader,
            "saved_user_source_count": len(user_reads),
            "saved_user_lineage_inherited_and_new_conversation_empty": user_reader,
            "approved_purpose_profile": purpose_profile,
            "approved_purpose_reads": purpose_reads,
            "purpose_version_changes_preserve_body": purpose_profile and catalog_update,
            "resource_profile": resource_profile,
            "resource_reads": resource_reads,
            "resource_body_excluded_from_prefix_and_catalog": resource_profile,
            "resource_public_pairs_hide_raw_ids_and_body": resource_profile,
            "reviewed_work_memory_reader_exercised": memory_reader,
            "reviewed_work_memory_read_count": len(memory_reads),
            "reviewed_work_memory_provenance_matches_archive": memory_reader,
            "controlled_sop_copy_exercised": copy_writer,
            "controlled_sop_copy_receipt_count": len(copy_receipts),
            "controlled_sop_copy_repeated_receipts_equal": copy_writer,
            "controlled_sop_copy_exact_body_and_draft_state": copy_writer,
            "controlled_sop_copy_body_absent_from_model": copy_writer,
            "copy_target_is_bound_to_each_work_outside_system_prefix": copy_writer,
            "copy_catalog_identical_between_work_items": copy_writer,
            "copy_target_ids_absent_from_tool_catalog": copy_writer,
            "historical_copy_not_reported_as_another_turns_effect": copy_writer,
            "copy_public_tool_pairs_hide_raw_ids_and_body": copy_writer,
            "sdk_version": "1.0.13",
            "runtime_versions": versions,
            "requests": requests,
            "normal_request_count": len(normal),
            "compaction_request_count": len(summaries),
            "compaction_classification": "Observed original FA checkpoint_compaction argument, forwarded unchanged.",
            "catalog_versions": [item["catalog_version"] for item in normal],
            "prior_archive_bytes_unchanged": True,
            "original_sdk_user_text_preserved": True,
            "same_conversation_resumed": True,
            "independent_conversation_used_new_session": True,
            "distinct_work_binding_count": len(questions),
            "compacted_state_restored_in_fresh_runtime": checkpoint,
            "server_clock_changed_between_turns": True,
            "ambient_capabilities_disabled": True,
            "exactly_one_system_message": True,
            "source_tool_canaries_observed_only_as_data": True,
            "archive_and_projection_versions_match": True,
            "actual_projection_accepted_by_strict_bridge_dto": True,
            "normal_sdk_stop_returns": len(stops),
            "force_stop_calls": len(force_stops),
            "paid_model_requests": 0,
            "cloud_requests": 0,
            "limitations": [
                "Exact FA-owned system text is required on every normal request, including after compaction.",
                "No provider cache-hit claim, token estimate, semantic quality or Azure deployment.",
                "The separately identified SDK summary request is not a normal FA prefix request or PA-04 acceptance.",
            ],
        }, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("original,replacement", [
    (None, "fa-instruction-v1"),
    ("fa-instruction-v1", "fa-instruction-skills-v1"),
    ("fa-instruction-skills-v1", None),
    ("fa-instruction-v1", "fa-instruction-user-turns-v1"),
    ("fa-instruction-user-turns-v1", None),
    ("fa-instruction-skills-v1", "fa-instruction-skill-purpose-v1"),
    ("fa-instruction-skill-purpose-v1", None),
    ("fa-instruction-v1", "fa-instruction-work-memory-v1"),
    ("fa-instruction-work-memory-v1", None),
    ("fa-instruction-v1", "fa-instruction-sop-copy-v1"),
    ("fa-instruction-sop-copy-v1", None),
])
def test_new_server_selector_cannot_silently_attach_an_old_installation(
        assistant, original, replacement, signing_key, jwk_client, request):
    add_skill(assistant)
    sop_copy = None
    if "fa-instruction-sop-copy-v1" in (original, replacement):
        http, _, _, version = approved_sop(assistant, signing_key, jwk_client)
        request.addfinalizer(http.close)
        sop_copy = TeamSopCopy.model_validate(copy_scope(version))
    if original == "fa-instruction-sop-copy-v1":
        h, control, _ = configured_copy(assistant, sop_copy)
    elif original == "fa-instruction-work-memory-v1":
        reviewed_note(assistant)
        h, control, _ = configured_memory(assistant)
    elif original == "fa-instruction-skill-purpose-v1":
        h, control, _ = configured_purpose(assistant)
    elif original == "fa-instruction-skills-v1":
        h, control, _ = configured_skills(assistant)
    else:
        h, control, _ = configured(assistant, original)
    receipt, _ = submit(h, "original-profile-request", "Keep the original configured instruction version.")
    before = conversation_view(h, receipt)
    profile = control.policy.profile.model_copy(update={
        "prompt_profile": replacement,
        "skill_artifacts": ("receipt-guide",) if replacement in {
            "fa-instruction-skills-v1", "fa-instruction-skill-purpose-v1",
        } else (),
        "sop_copy": sop_copy if replacement == "fa-instruction-sop-copy-v1" else None,
        "legacy_source_work": COPY_WORK if replacement == "fa-instruction-sop-copy-v1"
                              else control.policy.profile.legacy_source_work,
    })
    desired = SteadyWorkPolicy.model_validate_json(
        control.policy.model_copy(update={"profile": profile}).model_dump_json(),
    )
    restarted = WorkPolicyControl(h.runtime, desired, control.cost_reader)
    with pytest.raises(Problem) as denied:
        restarted.attach()
    assert denied.value.body["error"]["code"] == "WORK_POLICY_SCOPE_CHANGED"
    assert conversation_view(h, receipt) == before
    assert h.endpoint.requests == []
