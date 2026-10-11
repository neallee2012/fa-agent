from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import A
from shared_context.api import create_app
from shared_context.errors import Problem
from shared_context.hosted import HostedClient
from shared_context.m2_assistant import AssistantEngine
from shared_context.m2_copilot import ToolFailure
from shared_context.m3_tools import ArtifactTask
from shared_context.models import CreateWork
from test_m2_assistant import AGENT, command, hosted_runtime, status_until
from test_m3_artifacts import HUMAN, V1, V2, create as create_artifact, m3, upload as upload_version


def context_client(service):
    api = TestClient(create_app(service.context, lambda: A, artifact_service=service,
                                verified_artifact_identity_dependency=lambda: A))
    calls = []

    def send(request):
        calls.append((request.method, request.url.path, request.content))
        assert request.headers["Authorization"] == "Bearer synthetic-hosted-token"
        response = api.request(request.method, request.url.raw_path.decode(), content=request.content,
                               headers=dict(request.headers))
        return httpx.Response(response.status_code, content=response.content, headers=dict(response.headers))

    credential = SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token="synthetic-hosted-token"))
    return HostedClient(
        "https://context.example", service.context.auth.audience,
        "https://synthetic.table.core.windows.net", "workstate",
        "https://synthetic.services.ai.azure.com/api/projects/poc", "synthetic-memory",
        credential, httpx.Client(transport=httpx.MockTransport(send)),
    ), calls


def task(version=V1, mode="create"):
    return {"artifact_id": "report", "kind": "deliverable", "title": "Synthetic result",
            "version_id": version, "mode": mode, **({"base_version_id": V1} if mode == "revise" else {})}


def test_hosted_tools_deliver_real_api_bytes_and_revision_without_approval_capability(m3, tmp_path, monkeypatch):
    service, _, _ = m3
    hosted_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTEXT_M3_TOOLS_ENABLED", "true")
    client, calls = context_client(service)

    def runner(result_text, revision=False):
        async def sdk(**options):
            assert options["milestone"] == "m3"
            assert "Only entries explicitly marked approved" in options["prompt"]
            tools = {tool.name: tool for tool in options["tools"]}
            assert not {"publish", "handoff", "approve", "reject", "desktop_act"} & tools.keys()

            async def call(name, arguments=None):
                return await tools[name].handler({
                    "session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                    "tool_name": name, "arguments": arguments or {},
                })

            await call("read_context")
            if revision:
                source = await call("fa_read_artifact")
                assert "original total 40" in source.text
            else:
                await call("fa_create_artifact")
                await call("fa_create_artifact")
            await call("fa_write_file", {"name": "result.txt", "text": result_text})
            await call("update_work", {"step": "final", "progress": "file complete", "artifact_text": result_text})
            await call("fa_submit_artifact")
            await call("fa_submit_artifact")
            await call("finish_task")
            return {"sdk_session_id": options["sdk_session_id"], "history": [], "final_message": "file complete"}
        return sdk

    engine = AssistantEngine(client, home=tmp_path, process_marker="first", runner=runner("original total 40"))
    engine.dispatch(command("start", team=A.team, work="source-work", prompt="Produce a report.", input_text="12+20+8",
                            artifact_task=task()))
    status = status_until(engine, {"completed", "failed", "incomplete"})
    assert status["status"] == "completed", status
    assert status["artifact_result"]["sha256"] == hashlib.sha256(b"original total 40").hexdigest()
    first = service.get(HUMAN, A.team, "report").payload["versions"][0]
    assert len([item for item in calls if item[0] == "PUT"]) == 1
    assert len([item for item in calls if item[0] == "POST" and item[1].endswith("/artifacts/report")]) == 1

    new_home = tmp_path / "second-environment"
    new_home.mkdir()
    hosted_runtime(monkeypatch, new_home)
    service.context.create(A, A.team, "revision-work", CreateWork(goal="Revise report"))
    engine = AssistantEngine(client, home=new_home, process_marker="second", runner=runner("revised total 41", True))
    engine.dispatch(command("start", team=A.team, work="revision-work", prompt="Revise the total.", input_text="Add one.",
                            artifact_task=task(V2, "revise")))
    status = status_until(engine, {"completed", "failed", "incomplete"})
    assert status["status"] == "completed", status
    assert status["artifact_result"]["source_work"] == "revision-work"
    assert service.get(HUMAN, A.team, "report").payload["versions"][0] == first
    old = service.version(HUMAN, A.team, "report", V1)
    assert service.download(HUMAN, A.team, "report", V1, old.blob.etag)[1] == b"original total 40"
    assert len([item for item in calls if item[0] == "PUT"]) == 2


def test_artifact_task_cannot_enable_itself_or_request_human_powers(m3, tmp_path, monkeypatch):
    service, _, _ = m3
    hosted_runtime(monkeypatch, tmp_path)
    monkeypatch.delenv("CONTEXT_M3_TOOLS_ENABLED", raising=False)
    client, calls = context_client(service)
    engine = AssistantEngine(client, home=tmp_path, process_marker="disabled")
    with pytest.raises(Problem) as exc:
        engine.dispatch(command("start", team=A.team, work="source-work", prompt="Create.", input_text="", artifact_task=task()))
    assert exc.value.body["error"]["code"] == "M3_DISABLED"
    assert calls == []
    assert not (tmp_path / ".fa-m1" / "m2" / AGENT).exists()
    with pytest.raises(ValidationError):
        ArtifactTask.model_validate({**task(), "approve": True})
    with pytest.raises(ValidationError):
        ArtifactTask.model_validate(task(V1, "revise"))


@pytest.mark.parametrize("kind", ["deliverable", "sop", "skill"])
def test_submit_before_create_is_recoverable_without_recording_an_unknown_write(m3, tmp_path, monkeypatch, kind):
    service, _, _ = m3
    hosted_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTEXT_M3_TOOLS_ENABLED", "true")
    client, calls = context_client(service)

    async def sdk(**options):
        tools = {tool.name: tool for tool in options["tools"]}

        async def call(name, arguments=None):
            return await tools[name].handler({
                "session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                "tool_name": name, "arguments": arguments or {},
            })

        await call("read_context")
        await call("fa_write_file", {"name": "result.txt", "text": "Synthetic unapproved draft."})
        await call("update_work", {
            "step": "draft", "progress": "private draft ready", "artifact_text": "Synthetic unapproved draft.",
        })
        before_submit = len(calls)
        with pytest.raises(ToolFailure, match="M3_ARTIFACT_CREATE_REQUIRED"):
            await call("fa_submit_artifact")
        assert not any("/artifacts/" in path for _, path, _ in calls[before_submit:])
        await call("fa_create_artifact")
        await call("fa_submit_artifact")
        await call("finish_task")
        return {"sdk_session_id": options["sdk_session_id"], "history": [], "final_message": "Submitted, not approved"}

    engine = AssistantEngine(client, home=tmp_path, process_marker="create-precondition", runner=sdk)
    engine.dispatch(command("start", team=A.team, work="source-work", prompt="Create a synthetic draft.",
                            input_text="", artifact_task={**task(), "kind": kind}))
    status = status_until(engine, {"completed", "failed", "incomplete"})
    assert status["status"] == "completed", status
    assert len([item for item in calls if item[0] == "PUT"]) == 1
    assert service.get(HUMAN, A.team, "report").payload["versions"][0]["approval"] is None


@pytest.mark.parametrize("lost_action", ["create", "upload"])
def test_unknown_artifact_write_is_not_repeated_by_model_tools(m3, tmp_path, monkeypatch, lost_action):
    service, _, _ = m3
    hosted_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CONTEXT_M3_TOOLS_ENABLED", "true")
    client, calls = context_client(service)
    original = getattr(service, lost_action)

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise Problem(502, "SYNTHETIC_LOST_ACK", "artifact write accepted but acknowledgement lost")

    monkeypatch.setattr(service, lost_action, lose_response)

    async def sdk(**options):
        tools = {tool.name: tool for tool in options["tools"]}

        async def call(name, arguments=None):
            return await tools[name].handler({"session_id": options["sdk_session_id"], "tool_call_id": uuid4().hex,
                                             "tool_name": name, "arguments": arguments or {}})

        await call("read_context")
        await call("fa_write_file", {"name": "result.txt", "text": "Synthetic draft."})
        await call("update_work", {
            "step": "draft", "progress": "private draft ready", "artifact_text": "Synthetic draft.",
        })
        failing_tool = "fa_create_artifact" if lost_action == "create" else "fa_submit_artifact"
        if lost_action == "upload":
            await call("fa_create_artifact")
        with pytest.raises(Problem):
            await call(failing_tool)
        with pytest.raises(ToolFailure, match="ASSISTANT_EFFECT_UNKNOWN"):
            await call(failing_tool)
        with pytest.raises(ToolFailure, match="ASSISTANT_EFFECT_UNKNOWN"):
            await call("fa_submit_artifact")
        return {"sdk_session_id": options["sdk_session_id"], "history": [], "final_message": "Unknown; stopped"}

    engine = AssistantEngine(client, home=tmp_path, process_marker="unknown", runner=sdk)
    engine.dispatch(command("start", team=A.team, work="source-work", prompt="Create.", input_text="", artifact_task=task()))
    status = status_until(engine, {"completed", "failed", "incomplete"})
    assert status["status"] == "incomplete", status
    assert len([item for item in calls if item[0] == "POST" and item[1].endswith("/artifacts/report")]) == 1
    expected_versions = 1 if lost_action == "upload" else 0
    assert len([item for item in calls if item[0] == "PUT"]) == expected_versions
    assert len(service.get(HUMAN, A.team, "report").payload["versions"]) == expected_versions


def test_hosted_import_does_not_require_api_only_storage_auth_or_human_login_modules():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    program = """
import importlib.abc
import sys
sys.path.insert(0, sys.argv[1])
class BlockApiOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('azure.data', 'azure.storage', 'jwt', 'fastapi',
                   'shared_context.auth', 'shared_context.service', 'shared_context.storage',
                   'shared_context.m3_auth', 'shared_context.m3_artifacts', 'shared_context.m3_cli')
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('Hosted imported an API-only dependency: ' + fullname)
sys.meta_path.insert(0, BlockApiOnly())
import shared_context.hosted
print('Hosted imports only its runtime/client dependencies')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-I", "-c", program, str(source)],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_operator_probe_preserves_the_actual_api_denial_of_agent_approval(m3, monkeypatch):
    service, _, _ = m3
    create_artifact(service, "sop")
    record = upload_version(service)
    client, calls = context_client(service)
    path = "/teams/team-one/artifacts/report"
    monkeypatch.delenv("CONTEXT_M3_TOOLS_ENABLED", raising=False)
    with pytest.raises(Problem):
        client.dispatch({"operation": "api", "method": "GET", "path": path})
    assert calls == []
    monkeypatch.setenv("CONTEXT_M3_TOOLS_ENABLED", "true")
    assert client.dispatch({"operation": "api", "method": "GET", "path": path})["outcome"]["http_status"] == 200
    result = client.dispatch({
        "operation": "api", "method": "POST", "path": path + "/decisions", "etag": record.etag,
        "body": {"version_id": V1, "decision_id": V1, "sha256": record.payload["versions"][0]["sha256"], "decision": "approve"},
    })
    assert result["outcome"]["http_status"] == 403
    assert result["outcome"]["content"]["error"]["code"] == "M3_HUMAN_REQUIRED"
    assert service.get(HUMAN, A.team, "report") == record
