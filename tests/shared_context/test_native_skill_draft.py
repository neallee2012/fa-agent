"""C0 effect/source proof and SDK draft tool; API response is a named M3 transport double."""

import json
import hashlib

import httpx
import pytest
from starlette.testclient import TestClient
from types import SimpleNamespace

from conftest import A, TENANT
from shared_context import native_sdk
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture, material_digest
from shared_context.behavior_skill_draft import skill_purpose
from shared_context.m3_models import NativeArtifactSource
from shared_context.native_skill_draft import skill_draft_provider
from shared_context.native_skill_draft_wire import (
    NativeSkillDraftIntentRead, NativeSkillDraftReceipt, NativeSkillDraftWrite, artifact_id, draft_material,
)
from shared_context.native_turns import NativeTurnRuntime
from test_m2_copilot import runtime_path
from test_native_capabilities import selected_capture
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, harness, local_env, native_retry_policy


ARGUMENTS = {"purpose": "Recheck the original user procedure.", "body": "Read the original receipt before changing the next step.\n"}


def test_native_registry_has_one_skillify_consumer_and_keeps_other_providers():
    from shared_context.behavior_capabilities import available_grants
    from shared_context.native_capabilities import selected_providers

    providers = native_sdk.native_capability_providers("https://api.offline.invalid", lambda: "OFFLINE-agent")
    grant = available_grants(None, ("fa-skillify-v1",))["fa-skillify-v1"]
    assert len([provider for provider in providers if provider.accepts(grant)]) == 1
    assert selected_providers(selected_capture(), providers) == ()
    assert len(selected_providers(selected_capture("fa-skillify-v1", "fa-delegate-v1"), providers)) == 2


@pytest.mark.parametrize("lose_first_response", [False, True], ids=["saved", "original-receipt-lost"])
def test_sdk_skillify_reserves_original_slot_and_returns_same_m3_receipt_without_second_upload(
    local_env, monkeypatch, tmp_path, lose_first_response,
):
    calls, results, observations = [], [], []
    runtime = None

    async def api(request):
        calls.append(request.url.path)
        command = NativeSkillDraftWrite.model_validate_json(request.content)
        proof = await runtime.dispatch(NativeSkillDraftIntentRead(**command.model_dump()))
        assert proof.input_digest == material_digest(draft_material(command.arguments))
        original = await runtime.read(command.request_id)
        source = NativeArtifactSource(
            tenant_id=TENANT, team_id=A.team, agent_oid=A.oid, agent_id=proof.agent_id,
            conversation_id=proof.conversation_id, request_id=proof.request_id, submit_sha256=proof.submit_sha256)
        assert source.submit_sha256 == original.submit_digest and original.source is None
        receipt = NativeSkillDraftReceipt(
            native_source=source, artifact_id=artifact_id(TENANT, A.team, source.agent_id, source.conversation_id, source.request_id),
            version_id=material_digest(draft_material(command.arguments))[:32],
            sha256=hashlib.sha256(command.arguments.body.encode()).hexdigest(),
            byte_length=len(command.arguments.body.encode()), purpose=skill_purpose(command.arguments.purpose),
            metadata_sha256="d" * 64)
        if lose_first_response and len(calls) == 1:
            raise httpx.ReadTimeout("OFFLINE original M3 receipt lost", request=request)
        return httpx.Response(200, content=receipt.model_dump_json().encode())

    async def sdk(**options):
        tool = next(tool for tool in options["tools"] if tool.name == "save_skill_draft")
        for call in ("first-model-call", "same-draft-new-call"):
            try:
                result = await tool.handler({
                    "tool_call_id": call, "tool_name": tool.name, "session_id": options["sdk_session_id"],
                    "arguments": ARGUMENTS})
                results.append(NativeSkillDraftReceipt.model_validate_json(result.text))
            except native_sdk.ToolFailure as error:
                assert lose_first_response and call == "first-model-call"
                assert error.public_code == "NATIVE_SKILL_DRAFT_UNKNOWN"
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n"})
        observations.append("sdk-finished")
        return {"final_message": "Saved an unapproved draft for human review, not an active skill.",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    provider = skill_draft_provider(transport=httpx.MockTransport(api))
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "not-executed", api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-model", api_token_provider=lambda: "OFFLINE-agent",
        capability_providers=(provider,))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    command = NativeSubmit(command=submit().command, capture=selected_capture("fa-skillify-v1"))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(command).status_code == 202
        complete = harness.wait("rid-1", "completed")
        assert complete.status == "completed"
        assert results and all(value == results[0] for value in results) and results[0].approved is False
        assert len(calls) == (2 if lose_first_response else 1) and calls[0].endswith("/skill-draft/save")
        if lose_first_response:
            assert calls[1].endswith("/skill-draft/reconcile")
        assert observations == ["sdk-finished"]


@pytest.mark.parametrize("scenario", ["not-selected", "saved", "original-receipt-lost"])
def test_real_sdk_native_skillify_keeps_one_original_draft_across_tool_calls(
    harness, monkeypatch, tmp_path, runtime_path, scenario,
):
    from copilot import CopilotClient
    from shared_context import m2_copilot
    from sdk_checkpoint_local import sse
    from test_skill_isolation_native import child_environment

    requests, writes, api_calls = [], [], []

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            kwargs["env"] = child_environment(tmp_path / "private")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)

    async def api(request):
        api_calls.append(request.url.path)
        command = NativeSkillDraftWrite.model_validate_json(request.content)
        proof = await harness.runtime.dispatch(NativeSkillDraftIntentRead(**command.model_dump()))
        source = NativeArtifactSource(
            tenant_id=TENANT, team_id=A.team, agent_oid=A.oid, agent_id=proof.agent_id,
            conversation_id=proof.conversation_id, request_id=proof.request_id, submit_sha256=proof.submit_sha256)
        if request.url.path.endswith("/save"):
            writes.append(NativeSkillDraftReceipt(
                native_source=source, artifact_id=artifact_id(
                    TENANT, A.team, source.agent_id, source.conversation_id, source.request_id),
                version_id=material_digest(draft_material(command.arguments))[:32],
                sha256=hashlib.sha256(ARGUMENTS["body"].encode()).hexdigest(),
                byte_length=len(ARGUMENTS["body"].encode()), purpose=skill_purpose(ARGUMENTS["purpose"]),
                metadata_sha256="d" * 64))
            if scenario == "original-receipt-lost":
                raise httpx.ReadTimeout("OFFLINE original M3 receipt lost", request=request)
        assert len(writes) == 1
        return httpx.Response(200, content=writes[0].model_dump_json().encode())

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        tools = {entry["function"]["name"] for entry in body.get("tools", ())}
        if scenario == "not-selected":
            assert "save_skill_draft" not in tools and len(requests) == 1
            return sse(request, {"content": "No draft tool was selected or called."}, 1)
        assert "save_skill_draft" in tools
        if len(requests) <= 2:
            return sse(request, {"tool_calls": [{
                "index": 0, "id": f"draft-call-{len(requests)}", "type": "function",
                "function": {"name": "save_skill_draft", "arguments": json.dumps(ARGUMENTS)}}]}, len(requests))
        assert len(requests) == 3 and writes[0].artifact_id in json.dumps(body["messages"])
        return sse(request, {"content": "The original draft is saved, unapproved and not active."}, 3)

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-model", api_token_provider=lambda: "OFFLINE-agent",
        model_transport=httpx.MockTransport(model),
        capability_providers=(skill_draft_provider(transport=httpx.MockTransport(api)),))
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    captured = selected_capture() if scenario == "not-selected" else selected_capture("fa-skillify-v1")
    values = captured.model_dump()
    values["template"]["settings"]["model_provider"] = None
    values["model"] = {"id": "gpt-4.1-mini", "display_name": "Offline scripted model"}
    command = NativeSubmit(command=submit("native-real-draft").command, capture=NativeTurnCapture.model_validate(values))
    assert harness.invoke(command).status_code == 202
    completed = harness.wait(command.command.request_id, "completed")
    assert completed.usage.usage[0].requests == (1 if scenario == "not-selected" else 3)
    assert len(writes) == (0 if scenario == "not-selected" else 1)
    if scenario == "saved":
        assert api_calls == ["/work-policy/v1/native-capabilities/skill-draft/save"]
    elif scenario == "original-receipt-lost":
        assert api_calls == ["/work-policy/v1/native-capabilities/skill-draft/save",
                             "/work-policy/v1/native-capabilities/skill-draft/reconcile"]
    assert harness.invoke(command).json()["status"] == "completed"
    assert len(requests) == completed.usage.usage[0].requests
