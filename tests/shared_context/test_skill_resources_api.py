"""The manifest resource tool is opt-in and stays behind the original signed authority."""

import asyncio
import copy
import hashlib
import json
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A
from sdk_checkpoint_local import sse
from shared_context import m2_copilot
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_server import _CurrentAssistantProvider
from shared_context.behavior_skill_resources_prompt import SKILL_RESOURCES_CAPABILITIES, SKILL_RESOURCES_PROFILE
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, SkillPurpose, SkillResources, VersionInput
from shared_context.m3_skills import PurposeSkillReadResult
from test_approved_skill_api import configured as configured_legacy, post_skill
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models, runtime_path, sdk_io
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_skill_purpose import BODY, PURPOSE
from test_skill_resources import resource_fixture
from test_work_policy_lifecycle import enabled


def configured_resources(h):
    reference = resource_fixture(h.artifacts, source_work="legacy-source")
    created = h.artifacts.create(A, A.team, "resource-guide", CreateArtifact(
        kind="skill", title="Resource guide", source_work="legacy-source",
    ))
    metadata = VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(BODY).hexdigest(),
        skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE),
        skill_resources=SkillResources(entries=[reference]),
    )
    uploaded = h.artifacts.upload(A, A.team, "resource-guide", "1" * 32, created.etag, metadata, BODY)
    version = uploaded.payload["versions"][-1]
    h.artifacts.decide(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, "resource-guide",
        uploaded.etag, ApprovalInput(
            decision_id="1" * 32, version_id="1" * 32, sha256=version["sha256"],
            metadata_sha256=version["metadata_sha256"], decision="approve",
        ))
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({**profile.model_dump(),
            "prompt_profile": SKILL_RESOURCES_PROFILE, "skill_artifacts": ("resource-guide",)})
        for name, profile in h.runtime.profiles.items()
    })
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    return h, control, human, reference


def test_signed_resource_reader_matches_manifest_without_work_or_artifact_writes(assistant):
    h, control, human, reference = configured_resources(assistant)
    turn = open_turn(h, control, human, "resource-read")
    try:
        before = h.store._guard()
        artifacts = copy.deepcopy(h.artifacts.repository.data)
        skill = turn.client.read_skill("resource-guide")
        assert skill.schema_version == "fa-approved-skill-read-v3"
        assert skill.resources.entries == [reference]
        result = turn.client.read_skill_resource("resource-guide", "reference.json")
        assert result.resource == reference and result.text == '{"value":42}\n'
        assert result.skill_metadata_sha256 == skill.skill.metadata_sha256
        with pytest.raises(ValueError):
            PurposeSkillReadResult.model_validate_json(skill.model_dump_json())
        boundary = AssistantModelBoundary(None, text_reply=True, skill_access=True, skill_purpose=True,
                                          skill_resource_access=True)
        assert boundary.tool_result("read_skill", skill.model_dump_json()) == skill.model_dump_json()
        assert boundary.tool_result("read_skill_resource", result.model_dump_json()) == result.model_dump_json()
        old = AssistantModelBoundary(None, text_reply=True, skill_access=True, skill_purpose=True)
        with pytest.raises(KeyError):
            old.tool_result("read_skill_resource", result.model_dump_json())
        assert h.store._guard() == before and h.artifacts.repository.data == artifacts
        assert not h.endpoint.requests
        with pytest.raises(Problem) as denied:
            turn.client.read_skill_resource("resource-guide", "unlisted.json")
        assert denied.value.body["error"]["code"] == "M3_SKILL_RESOURCE_DENIED"
    finally:
        turn.client.close()


def test_deactivated_skill_cannot_read_a_previous_resource_manifest(assistant):
    from shared_context.m3_models import SkillDeactivationInput
    h, control, human, _ = configured_resources(assistant)
    turn = open_turn(h, control, human, "resource-revoked")
    try:
        assert turn.client.read_skill_resource("resource-guide", "reference.json").text
        record = h.artifacts.get(A, A.team, "resource-guide")
        version = record.payload["versions"][-1]
        h.artifacts.deactivate_skill(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team,
            "resource-guide", record.etag, SkillDeactivationInput(
                schema_version="fa-skill-deactivation-v1",
                decision_id="2" * 32, version_id="1" * 32, sha256=version["sha256"],
                metadata_sha256=version["metadata_sha256"],
            ))
        before = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client.read_skill_resource("resource-guide", "reference.json")
        assert denied.value.body["error"]["code"] == "SKILL_SOURCE_CHANGED"
        assert h.store._guard() == before and not h.endpoint.requests
    finally:
        turn.client.close()


def test_old_skill_profile_cannot_enable_resources_by_calling_the_endpoint(assistant):
    from test_approved_skill_api import add_skill
    add_skill(assistant)
    h, control, human = configured_legacy(assistant)
    turn = open_turn(h, control, human, "old-resource-denial")
    try:
        response = post_skill(turn, {
            "request_id": "read-skill-resource", "name": "receipt-guide", "resource": "reference.json",
        }, action="read-skill-resource")
        assert response.status_code == 403
        assert response.headers["x-behavior-assistant-error-code"] == "SKILL_ACCESS_DISABLED"
        assert turn.client.read_skill("receipt-guide").skill.name == "receipt-guide"
    finally:
        turn.client.close()


def test_hosted_uses_only_the_resource_profile_tool_set_across_cold_continuation(assistant, monkeypatch, tmp_path):
    h, control, _, _ = configured_resources(assistant)
    calls = sdk_io(monkeypatch, expected_tools=SKILL_RESOURCES_CAPABILITIES)
    factory = m2_copilot.CopilotClient.side_effect
    seen = []
    def capture(**connection):
        sdk = factory(**connection)
        original = sdk.create_session.side_effect
        async def opened(*args, **options):
            seen.append(options)
            return await original(*args, **options)
        sdk.create_session.side_effect = opened
        sdk.resume_session.side_effect = opened
        return sdk
    m2_copilot.CopilotClient.side_effect = capture
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Resource profile ready."}, {"content": "Original context retained."}])
    first, _ = submit(h, "resource-first", "Use the approved guide and resource.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    h.executor.root = tmp_path / "new-resource-sdk-files"
    second, _ = submit(h, "resource-second", "Continue the original task.", first["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    assert calls[1]["resume"] and calls[0]["sdk_session_id"] == calls[1]["sdk_session_id"]
    for options in seen:
        tools = {tool.name: tool for tool in options["tools"]}
        assert set(tools) == set(SKILL_RESOURCES_CAPABILITIES)
        assert set(tools["read_skill_resource"].parameters["properties"]) == {"name", "resource"}
        assert options["enable_config_discovery"] is False and options["mcp_servers"] == {}
    check_history(h)


def test_model_gateway_does_not_accept_resource_tool_without_explicit_profile_capability():
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions", object())
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Read data."}],
            "tools": [{"type": "function", "function": {"name": "read_skill_resource", "parameters": {}}}]}
    with pytest.raises(Problem) as denied:
        gateway.validate(body, text_reply=True, model="gpt-4.1-mini", skill_access=True)
    assert denied.value.body["error"]["code"] == "ASSISTANT_TOOLS_DENIED"
    gateway.validate(body, text_reply=True, model="gpt-4.1-mini", skill_access=True, skill_resource_access=True)


def test_production_provider_receives_resource_capability_after_original_model_consumption(assistant):
    h, control, human, _ = configured_resources(assistant)
    sent = []
    def provider(request):
        sent.append(json.loads(request.content))
        return sse(request, {"content": "Production provider resource wiring."}, 1)
    composition = SimpleNamespace(
        config=SimpleNamespace(assistant=SimpleNamespace(
            provider_endpoint="https://provider.invalid/openai/v1/chat/completions")),
        runtime=h.runtime,
    )
    gateway = _CurrentAssistantProvider(
        composition, SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_RESOURCE_PROVIDER")),
        transport=httpx.MockTransport(provider),
    )
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    with TestClient(h.app) as client:
        h.client = client
        turn = open_turn(h, control, human, "resource-production-provider")
        try:
            body = {"model": "gpt-4.1-mini", "stream": True,
                    "messages": [{"role": "user", "content": "Read the approved resource."}],
                    "tools": [{"type": "function", "function": {
                        "name": "read_skill_resource", "description": "Read approved manifest data.",
                        "parameters": {"type": "object", "properties": {}},
                    }}]}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            turn.client.reserve_model(digest)
            response = turn.client.model(httpx.Request(
                "POST", "https://context.invalid/model/v1/chat/completions", json=body,
            ))
            assert response.status_code == 200
            assert len(sent) == 1 and sent[0]["tools"][0]["function"]["name"] == "read_skill_resource"
        finally:
            turn.client.close()
    asyncio.run(gateway.aclose())


def test_production_provider_forwards_existing_work_memory_capability_without_weakening_last_gate():
    sent, checks = [], []
    def provider(request):
        sent.append(json.loads(request.content))
        return sse(request, {"content": "Existing work memory capability."}, 1)
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://provider.invalid/openai/v1/chat/completions")))
    gateway = _CurrentAssistantProvider(
        composition, SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_MEMORY_PROVIDER")),
        transport=httpx.MockTransport(provider),
    )
    body = {"model": "gpt-4.1-mini", "stream": True,
            "messages": [{"role": "user", "content": "Read reviewed memory."}],
            "tools": [{"type": "function", "function": {"name": "read_work_memory", "parameters": {}}}]}
    async def current():
        checks.append(True)
    async def run():
        try:
            response = await gateway.send(body, before_send=current, text_reply=True, model="gpt-4.1-mini",
                                          work_memory_access=True)
            await response.aread()
        finally:
            await gateway.aclose()
    asyncio.run(run())
    assert len(sent) == 1 and checks
