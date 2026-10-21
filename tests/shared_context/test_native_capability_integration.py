"""C6 vertical traces through the native factory, real consumers, SDK and original API.

Only model/HTTP/storage I/O and the Linux OS probe are synthetic. The executable is
the pinned Windows SDK runtime; a Linux profile trace is not Linux/Hosted evidence.
"""

import asyncio
from contextlib import ExitStack, contextmanager
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

from copilot import CopilotClient
from fastapi import FastAPI
import httpx
import pytest
from starlette.testclient import TestClient

from conftest import InMemoryRepository
from shared_context import behavior_sdk_files, m2_copilot, native_sdk
from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings, TemplateSkill
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_hosted import create_host
from shared_context.behavior_instruction_models import InstructionModel
from shared_context.behavior_native_capture import project_capture
from shared_context.behavior_native_sessions import native_session_id, native_sessions
from shared_context.behavior_native_turn_wire import (
    NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER, NativeCancel, NativeCandidateRead, NativeConversation, NativeConversationRead,
    NativeRead, NativeResult, NativeSessionFollow, NativeSessionPreparation, NativeSessionPrepare,
    NativeSessionRead, NativeSubmit,
)
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_wire import HostedIdentity
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_capabilities import (
    BUILTIN_CAPABILITIES, DELEGATE_READER_ENTRY_ID, SKILL_RESOURCES_ENTRY_ID,
    available_grants, check_selectable, enabled_toolboxes,
)
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, VersionInput
from shared_context.tool_catalog import load_catalog
from hosted_versions_stub import deployed_versions
from sdk_checkpoint_local import sse
from test_agent_runtime import peer_runtime  # noqa: F401
from test_agent_runtime_routing import agent_headers
from test_approved_skill_api import BODY as SKILL_BODY
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_instruction_responses_native import response_bytes
from test_m2_copilot import runtime_path  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_delegation import TASK
from test_native_session_prepare_api import platform_sessions
from test_native_turn_api import Credential
from test_native_turns import local_env  # noqa: F401
from test_skill_isolation_native import child_environment
from test_sdk_compaction_product import SUMMARY


API_URL = "https://api.offline.invalid"
MODEL_ROOT = "https://offline-fixture.services.ai.azure.com/api/projects/offline"
TEXT = "Remember this preference: Use short status summaries.\nCompare 850 and 320."
QUOTE = "Use short status summaries."
A_SUBSET = ("fa-delegate-v1", "fa-memory-candidate-v1", "fa-skills-v1")
CATALOG_ROOT = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "tool_catalog"


def deployed_catalog():
    """The committed catalog as the API loads it by default: N5 toolbox entries stay off."""
    return enabled_toolboxes(load_catalog(CATALOG_ROOT), ())


def api_application(composition):
    app = FastAPI()
    _install_error_handlers(app)
    install_work_policy_routes(app, composition)
    if composition.agent_id is None:
        install_peer_routing(app, composition, api_application)
    return app


@pytest.fixture
def integrated(request, local_env, monkeypatch, tmp_path, runtime_path, signing_key):
    real_connect, real_popen = socket.socket.connect, subprocess.Popen
    peer = request.getfixturevalue("peer_runtime") if getattr(request, "param", None) == "peers" else None
    case = peer.c if peer is not None else request.getfixturevalue("synthetic_standing_v1")

    def loopback(sock, address):
        assert isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}
        return real_connect(sock, address)

    class PinnedProcess(real_popen):
        def __init__(self, args, *positional, **keywords):
            assert not keywords.get("shell")
            assert isinstance(args, (list, tuple)) and Path(args[0]).resolve() == runtime_path.resolve()
            super().__init__(args, *positional, **keywords)

    monkeypatch.setattr(socket.socket, "connect", loopback)
    monkeypatch.setattr(subprocess, "Popen", PinnedProcess)
    private_env = child_environment(tmp_path / "private")
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(name, private_env[name])
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                 "FA_PRIVATE_MEMORY_STORE"):
        monkeypatch.delenv(name, raising=False)

    class PrivateClient(CopilotClient):
        def __init__(self, *args, **kwargs):
            assert kwargs["use_logged_in_user"] is False and kwargs["mode"] == "empty"
            kwargs["env"] = {**private_env, **kwargs["env"]}
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(m2_copilot, "CopilotClient", PrivateClient)
    composition = case.h.composition
    case.authority.artifacts.repository = InMemoryRepository()
    target = case.native.targets[0]
    # #534: a native reader is the current version's own session, so the offline Hosted session is version-specific.
    from shared_context.behavior_agent_versions import current_hosted_version
    version = current_hosted_version(composition.runtime, target)
    target = target.model_copy(update={"agent_version": version, "session_id": native_session_id(
        target, composition.config.identity.managed_identity_client_id, version=version)})
    identity = HostedIdentity(
        agent_id=target.agent_id, agent_name=target.agent_name,
        agent_version=target.agent_version, session_id=target.session_id,
    )
    platform = {target.session_id: target}
    platform_sessions(monkeypatch, platform)
    token = agent_headers(case, signing_key, 0)["Authorization"].removeprefix("Bearer ")

    class OfflineCredential:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get_token(self, scope):
            assert scope in {"api://OFFLINE/.default", "https://ai.azure.com/.default"}
            return SimpleNamespace(token=token if scope.startswith("api://") else "OFFLINE-model-token")

    monkeypatch.setattr("azure.identity.aio.ManagedIdentityCredential", lambda **_: OfflineCredential())
    hosted = SimpleNamespace(
        identity=identity, api=SimpleNamespace(api_url=API_URL, audience="api://OFFLINE"),
        supervisor=SimpleNamespace(journal=SimpleNamespace(root=tmp_path / "agent")),
    )
    with monkeypatch.context() as environment:
        environment.setenv("FA_NATIVE_TURNS", NATIVE_STATE_PROFILE)
        environment.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-factory-configuration")
        environment.setenv("AGENTSERVER_TASKS_BACKEND", "hosted")
        environment.setenv("BEHAVIOR_ASSISTANT_RUNTIME_PATH", str(runtime_path))
        environment.setenv("FOUNDRY_PROJECT_ENDPOINT", MODEL_ROOT)
        runtime = native_sdk.native_runtime_from_environment(hosted)
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", target.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", target.agent_name)
    monkeypatch.setenv("FA_NATIVE_TURNS", NATIVE_STATE_PROFILE)

    api = api_application(composition)
    api_calls, bindings, closed, sdk_calls, sdk_outcomes, errors = [], [], [], [], [], []
    api_response = [lambda _request, response: response]

    class ApiTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.host == "api.offline.invalid"
            response = await httpx.ASGITransport(app=api).handle_async_request(request)
            await response.aread()
            api_calls.append((request.url.path, json.loads(request.content) if request.content else None,
                              response.status_code))
            return api_response[0](request, response)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: ApiTransport())
    monkeypatch.setattr("httpx._client.AsyncHTTPTransport", lambda **_: ApiTransport())
    real_run = native_sdk.run_copilot

    async def observed_run(**options):
        sdk_calls.append(options)
        try:
            outcome = await real_run(**options)
        except BaseException as error:
            errors.append((type(error).__name__, str(error)))
            raise
        sdk_outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(native_sdk, "run_copilot", observed_run)
    providers = runtime.executor.capability_providers

    def observed_provider(provider):
        async def bind(context, grants):
            binding = await provider.bind(context, grants)
            selected = tuple(grant.entry_id for grant in grants)
            bindings.append((selected, context, binding))

            async def close():
                if binding.close is not None:
                    await binding.close()
                closed.append(selected)

            return replace(binding, close=close)
        return replace(provider, bind=bind)

    runtime.executor.capability_providers = tuple(observed_provider(provider) for provider in providers)
    with ExitStack() as host_lifetime:
        host_options = {}
        if getattr(request, "param", None) == "responses":
            from shared_context.native_responses import native_responses_handler_factory
            monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
            hosted.supervisor.journal.root.mkdir(parents=True, exist_ok=True)
            host_options["responses_handler_factory"] = native_responses_handler_factory
        client = host_lifetime.enter_context(TestClient(create_host(hosted, native_runtime=runtime, **host_options)))
        servers = {identity.session_id: (client, identity)}

        def invoke(command):
            return client.post("/invocations", json=command.model_dump(mode="json"), headers={
                NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE,
                "x-agent-session-id": identity.session_id,
                "x-agent-invocation-id": "offline-" + uuid4().hex,
                "x-agent-foundry-call-id": "C6-original-call",
            })

        def remote(request):
            value = json.loads(request.content)
            destination, destination_identity = servers[request.url.params["agent_session_id"]]
            response = destination.post("/invocations", json=value, headers={
                NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE,
                "x-agent-session-id": destination_identity.session_id,
                "x-agent-invocation-id": "offline-read-" + uuid4().hex,
                "x-agent-foundry-call-id": "C6-api-read",
            })
            return httpx.Response(response.status_code, content=response.content, headers={
                "content-type": "application/json", "x-agent-session-id": destination_identity.session_id,
                "x-agent-invocation-id": "offline-read-result",
            })

        foundry = FoundryHttpClient(target, Credential(), transport=httpx.MockTransport(remote))
        original_clients = composition.clients
        composition.clients = tuple(item for item in original_clients if not isinstance(item, FoundryHttpClient)) + (foundry,)

        async def finished(rid):
            async with asyncio.timeout(90):
                while True:
                    receipt = await runtime.read(rid)
                    if receipt.status in {"completed", "unknown", "busy_refused"} and any(
                            path.endswith("/turn-usage") and body["request_id"] == rid and status == 200
                            for path, body, status in api_calls):
                        return receipt
                    await asyncio.sleep(0.02)

        try:
            state = invoke(NativeSessionRead())
            assert state.status_code == 200, state.text
            assert state.json()["preparation"] is None
            prepared = invoke(NativeSessionPrepare(
                request_id="C6-prepared", expected_session_id=identity.session_id,
                expected_etag=state.json()["etag"]))
            assert prepared.status_code == 200, prepared.text
            assert native_sessions(composition).current(foundry, refresh=True).target == target
            yield SimpleNamespace(
                case=case, peer=peer, runtime=runtime, identity=identity, providers=providers,
                call=client.portal.call, invoke=invoke, finished=finished, api_calls=api_calls,
                bindings=bindings, closed=closed, sdk_calls=sdk_calls, sdk_outcomes=sdk_outcomes, errors=errors,
                target=target, hosted=hosted, servers=servers, close=host_lifetime.close,
                foundry=foundry, observe_provider=observed_provider,
                api_response=api_response, platform=platform,
            )
        finally:
            composition.clients = original_clients
            foundry.close()


def a_capture(case, selected=A_SUBSET, *, catalog=None):
    catalog = deployed_catalog() if catalog is None else catalog
    # A's ten: #478's child reader needs a base that declares read_user_file, which fa-instruction-v3 does not.
    every = available_grants(catalog, tuple(BUILTIN_CAPABILITIES))
    with pytest.raises(Problem) as unsupported:
        check_selectable(tuple(every[name].ref for name in sorted(every)), "fa-instruction-v3", every)
    assert unsupported.value.body["error"]["code"] == "CAPABILITY_BASE_UNSUPPORTED"
    grants = available_grants(catalog, tuple(name for name in BUILTIN_CAPABILITIES
                                            if name not in {DELEGATE_READER_ENTRY_ID, SKILL_RESOURCES_ENTRY_ID}))
    selected = tuple(grants) if selected is None else selected
    case.h.runtime.capability_grants, case.h.runtime.capability_catalog = grants, catalog
    actor = case.authority.actors[case.control.policy.template.agent_oid]
    artifacts = case.authority.artifacts
    raw = SKILL_BODY.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    created = artifacts.create(actor, actor.team, "receipt-guide", CreateArtifact(
        kind="skill", title="Receipt verification guide", source_work="legacy-source"))
    uploaded = artifacts.upload(actor, actor.team, "receipt-guide", "1" * 32, created.etag, VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=sha), raw)
    artifacts.decide(HumanApprover(artifacts.approval.approver_oid, actor.team), actor.team, "receipt-guide",
                     uploaded.etag, ApprovalInput(decision_id="1" * 32, version_id="1" * 32,
                                                  sha256=sha, decision="approve"))
    skills = case.authority.skills.catalog(actor, actor.team, ("receipt-guide",),
                                          schema_version="fa-approved-skill-catalog-v2")
    role = assemble_prompt("fa-instruction-v3").record
    template = AgentTemplate(
        role=TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                          capability_catalog_version=role.capability_catalog_version),
        settings=TemplateSettings(
            checkpoint_profile={"name": "manual-no-custom-v1", "platform": "linux-x64", "provider": "openai-responses"},
            model_provider="openai-responses", sdk_start_profile="fa-sdk-start-gate-v1",
            turn_origin_profile="fa-instruction-context-origin-v1", model_capacity_profile="fa-model-capacity-v1",
            text_attachment_profile="fa-source-text-attachments-v1", user_text_file_profile="fa-user-text-files-v1",
            capabilities=tuple(grants[name].ref for name in sorted(grants)),
        ),
        skills=tuple(TemplateSkill(name=entry.name, version_id=entry.version_id, sha256=entry.sha256,
                                   metadata_sha256=entry.metadata_sha256) for entry in skills.entries),
    )
    model = InstructionModel(
        id="gpt-5.4-mini", display_name="Synthetic Responses model", context_window_tokens=64000,
        max_output_tokens=2048, supports_image_input=True, supports_reasoning_summary=True,
        input_microusd_per_million=400000, output_microusd_per_million=1600000,
    )
    case.control.models.models[model.id] = model
    return project_capture(
        template, "C6-original-A-selection", model,
        head_refs=tuple(grants[name].ref for name in sorted(selected)),
        head_selection_id="C6-current-subset", available=grants, count_tokens=lambda _: 0,
    )


def tool_result(body, call_id):
    output = next(item["output"] for item in body["input"]
                  if item.get("type") == "function_call_output" and item["call_id"] == call_id)
    if isinstance(output, list):
        output = next(item["text"] for item in output if item.get("type") in {"input_text", "text"})
    return json.loads(output)


def b_capture(case):
    role = assemble_prompt("fa-instruction-v1").record
    template = AgentTemplate(
        role=TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                          capability_catalog_version=role.capability_catalog_version),
        settings=TemplateSettings(),
    )
    return project_capture(
        template, "C6-original-B-selection", case.control.models.select("gpt-4.1-mini"),
        available=available_grants(deployed_catalog(), tuple(BUILTIN_CAPABILITIES)),
        count_tokens=lambda _: 0,
    )


@contextmanager
def original_peer_transport(h, monkeypatch):
    from azure.ai.projects.models import VersionRefIndicator
    from azure.core.exceptions import ResourceNotFoundError
    from shared_context import native_agent_api
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from test_native_agent_api import AgentTransport

    b = h.peer.b
    select_profile(b.runtime.work_policy, b.assistant.authority, h.case.human, SelectConversationProfile(
        request_id="C6-B-original-selection", expected_selection_id=None, template=b_capture(h.case).template))
    target = b.runtime.dep.native_launches.targets[0]
    from shared_context.behavior_agent_versions import current_hosted_version
    version = current_hosted_version(b.runtime, target)
    target = target.model_copy(update={"agent_version": version, "session_id": native_session_id(
        target, b.config.identity.managed_identity_client_id, version=version)})
    remote = AgentTransport(target)
    remote.preparation = NativeSessionPreparation(
        agent_id=target.agent_id, request_id="C6-B-prepared", previous_hosted_session_id=None,
        hosted_session_id=target.session_id, hosted_agent_version=target.agent_version)
    native_sessions(b).prepared = remote.preparation
    native_sessions(b).binding = target
    foundry = FoundryHttpClient(target, Credential(), transport=httpx.MockTransport(remote))
    originals = b.clients
    b.clients = tuple(item for item in originals if not isinstance(item, FoundryHttpClient)) + (foundry,)
    repository = InMemoryRepository()
    monkeypatch.setattr(native_agent_api, "TableRepository", lambda _: repository)
    targets = {(item.agent_name, item.session_id): item for item in (h.target, target)}

    class PlatformClient:
        def __init__(self, **_):
            self.agents = self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_session(self, name, sid):
            assert name in {item.agent_name for item in targets.values()}
            item = targets.get((name, sid))
            if item is None:
                raise ResourceNotFoundError("No synthetic version-specific session.")
            return SimpleNamespace(agent_session_id=sid, status="active",
                                   version_indicator=VersionRefIndicator(agent_version=item.agent_version))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", PlatformClient)
    try:
        yield SimpleNamespace(target=target, remote=remote)
    finally:
        b.clients = originals
        foundry.close()


@contextmanager
def original_native_console(h, tmp_path, signing_key):
    from shared_context.behavior_assistant import _LocalHttp
    from shared_context.behavior_inbox import INBOX_VERSION
    from web_test_console.adapter import parse_config
    from web_test_console.bridge import create_app as console_app
    from web_test_console.instructions import InstructionConnection
    from test_web_test_console_standing import configuration

    state = h.invoke(NativeSessionRead()).json()
    prepared = h.invoke(NativeSessionPrepare(
        request_id="C6-prepared", expected_session_id=h.identity.session_id, expected_etag=state["etag"]))
    assert prepared.status_code == 200, prepared.text
    wrapper = SimpleNamespace(config=SimpleNamespace(channel=SimpleNamespace(version=INBOX_VERSION)),
                              runtime=h.case.h.runtime, verifier=h.case.h.composition.verifier)
    config = parse_config(json.dumps(configuration(wrapper, h.case.control, ids=("C6-unused-bookmark",))).encode())
    connection = InstructionConnection(
        schema_version="fa-console-instructions-connection-v1", configuration_sha256=config.sha256,
        backend_core_sha256=config.backend_core_sha256, state_profile="foundry-native-v1")  # the workbench lane
    calls, transform = [], [lambda _request, response: response]
    with TestClient(api_application(h.case.h.composition)) as api:
        local = _LocalHttp(api)

        def forward(request):
            response = local.handle_request(request)
            calls.append((request.method, request.url.path, response.status_code))
            return transform[0](request, response)

        with httpx.Client(transport=httpx.MockTransport(forward)) as backend:
            app = console_app(
                config, tmp_path / "native-console.sqlite3", backend=backend, port=8877,
                token_provider=lambda: h.case.h.headers(signing_key)["Authorization"].removeprefix("Bearer "),
                clock=lambda: h.case.h.time, environment="OFFLINE_TEST", instruction_connection=connection)
            with TestClient(app, base_url="http://127.0.0.1:8877") as ui:
                assert ui.get("/").status_code == 200
                headers = {"Origin": "http://127.0.0.1:8877",
                           "X-Console-CSRF": ui.get("/api/state").json()["csrf"]}
                authenticated = ui.post("/api/authenticate", json={}, headers=headers)
                assert authenticated.status_code == 200, authenticated.text
                yield SimpleNamespace(client=ui, headers=headers, calls=calls, transform=transform, app=app)


def test_a_subset_preserves_full_template_and_original_result_with_real_consumers(integrated, monkeypatch):
    # Only OS detection is doubled: this verifies the Linux profile mapping, not Linux execution.
    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    h = integrated
    captured = a_capture(h.case)
    original_template = captured.template.model_dump_json()
    requests = []

    async def model(request):
        assert str(request.url) == MODEL_ROOT + "/openai/v1/responses"
        body = json.loads(request.content)
        requests.append(body)
        count = len(requests)
        assert count <= 6, "The tracer permits only the original parent/child model calls."
        assert body["model"] == "gpt-5.4-mini" and body["store"] is False
        assert "previous_response_id" not in body
        names = {tool["name"] for tool in body.get("tools", ())}
        if count == 5:
            assert names == set()
            assert TASK["prompt"] in json.dumps(body["input"])
        else:
            assert {"read_context", "read_skill", "stage_memory_candidate", "task"} <= names
            assert not ({"run_command", "desktop_observe", "save_skill_draft", "deliver_image",
                         "send_agent_message", "WebIQ-MCP-web", "playwright-browser_navigate"} & names)
        tool, arguments, text = None, None, None
        if count == 1:
            assert TEXT in json.dumps(body["input"]).replace("\\n", "\n")
            tool = "read_context"
        elif count == 2:
            origin = tool_result(body, "call_1")
            assert origin["content"] == TEXT
            assert origin["turn_origin"]["request_id"] == "C6-A-original-rid"
            assert origin["turn_origin"]["source_kind"] == "human"
            assert origin["turn_origin"]["authorization"] is False
            tool, arguments = "read_skill", {"name": "receipt-guide"}
        elif count == 3:
            assert tool_result(body, "call_2")["text"] == SKILL_BODY
            tool, arguments = "stage_memory_candidate", {
                "decision": "candidate", "reason": "stable_preference", "quote": QUOTE,
            }
        elif count == 4:
            staged = tool_result(body, "call_3")
            assert staged["state"] == "staged" and staged["effective"] is False
            pending = await h.runtime.dispatch(NativeCandidateRead(
                conversation_id=staged["conversation_id"], request_id="C6-A-original-rid"))
            assert pending.state == "awaiting_saved_turn" and pending.text is None
            tool, arguments = "task", TASK
        elif count == 5:
            text = "Child verified the original public sample: 530."
        else:
            bridge = next(binding.delegation_runtime for _, _, binding in h.bindings
                          if binding.delegation_runtime is not None)
            child = await bridge.read()
            assert child.parent_request_id == "C6-A-original-rid" and child.status == "completed"
            assert child.result == "Child verified the original public sample: 530."
            text = "C6 original result: 530."
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=response_bytes(count, tool=tool, arguments=arguments, text=text))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    command = NativeSubmit(command=InstructionSubmit(request_id="C6-A-original-rid", content=TEXT), capture=captured)
    response = h.invoke(command)
    assert response.status_code == 202, response.text
    receipt = h.call(h.finished, command.command.request_id)
    assert receipt.status == "completed", (receipt.failure_code, len(requests), h.errors, h.api_calls)
    assert receipt.capture == captured and receipt.command == command.command and receipt.source is None
    assert receipt.delivered is False
    assert receipt.capture.template.model_dump_json() == original_template
    assert len(receipt.capture.template.settings.capabilities) == 10
    assert tuple(grant.entry_id for grant in receipt.capture.capabilities.grants) == A_SUBSET
    assert {selected for selected, _, _ in h.bindings} == {(name,) for name in A_SUBSET}
    assert h.closed == [selected for selected, _, _ in reversed(h.bindings)]
    page = h.call(h.runtime.result, NativeResult(conversation_id=receipt.conversation_id,
                                               request_id=receipt.request_id))
    assert page.complete and page.text == "C6 original result: 530."
    assert page.result.text_sha256 == hashlib.sha256(b"C6 original result: 530.").hexdigest()
    candidate = h.call(h.runtime.dispatch, NativeCandidateRead(conversation_id=receipt.conversation_id,
                                                              request_id=receipt.request_id))
    assert candidate.state == "pending_review" and candidate.effective is False and candidate.text == QUOTE
    assert (candidate.source.start_byte, candidate.source.end_byte) == (26, 53)
    assert candidate.source.original_request_id == receipt.request_id
    usage = receipt.usage.usage
    assert receipt.usage.state == "complete" and len(usage) == 1
    assert (usage[0].requests, usage[0].measured_requests, usage[0].input_tokens, usage[0].output_tokens) == (6, 6, 126, 78)
    assert h.sdk_calls[0]["native_checkpoint_turn"].capture_digest == captured.sha256
    assert h.sdk_calls[0]["model_capacity"] == captured.model_capacity
    assert h.sdk_outcomes[0]["sdk_stop"]["graceful"] is True
    assert h.invoke(command).json()["status"] == "completed"
    assert len(requests) == 6 and len(h.sdk_calls) == 1 and len(h.bindings) == 3
    reports = [(body, status) for path, body, status in h.api_calls if path.endswith("/turn-usage")]
    assert reports == [({"request_id": receipt.request_id, "usage": receipt.usage.model_dump(mode="json")}, 200)]
    assert h.case.control.records == {}


def test_b_v1_completions_does_not_inherit_optional_capabilities_from_the_team_catalog(integrated):
    h = integrated
    captured, requests = b_capture(h.case), []

    async def model(request):
        assert str(request.url) == MODEL_ROOT + "/openai/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        assert len(requests) == 1 and body["model"] == "gpt-4.1-mini"
        names = {tool["function"]["name"] for tool in body["tools"]}
        assert {"read_context", "execute_operation"} <= names
        assert not {"task", "read_skill", "run_command", "stage_memory_candidate", "save_skill_draft",
                    "deliver_image", "send_agent_message"} & names
        assert not any(name.startswith(("WebIQ-MCP-", "playwright-", "desktop_")) for name in names)
        return sse(request, {"content": "B kept its own Completions configuration."}, 1)

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    command = NativeSubmit(command=InstructionSubmit(request_id="C6-B-rid", content="Use B's original settings."),
                           capture=captured)
    assert h.invoke(command).status_code == 202
    receipt = h.call(h.finished, command.command.request_id)
    assert receipt.status == "completed", (receipt.failure_code, h.errors, h.api_calls)
    assert receipt.capture == captured and not receipt.delivered
    assert captured.capabilities is None and captured.template.skills == ()
    assert captured.template.settings.model_dump(mode="json") == {
        "sdk_version": "1.0.13", "runtime_version": "1.0.83", "mode": "empty",
        "checkpoint_profile": None, "sdk_start_profile": None, "model_capacity_profile": None,
    }
    assert h.bindings == h.closed == []
    assert len(h.sdk_calls) == 1 and "capability_session" not in h.sdk_calls[0]
    assert h.sdk_calls[0]["model_provider"] is None
    page = h.call(h.runtime.result, NativeResult(conversation_id=receipt.conversation_id,
                                               request_id=receipt.request_id))
    assert page.text == "B kept its own Completions configuration."
    usage = receipt.usage.usage[0]
    assert (usage.wire_api, usage.requests, usage.measured_requests, usage.input_tokens, usage.output_tokens) == (
        "completions", 1, 1, 1500, 100)
    assert h.invoke(command).json()["status"] == "completed" and len(requests) == 1


def test_cancelled_a_child_keeps_its_original_effect_and_does_not_cancel_the_other_cid(integrated, monkeypatch):
    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    h = integrated
    captured = a_capture(h.case)
    parent = NativeSubmit(command=InstructionSubmit(request_id="C6-cancelled-A", content=TEXT), capture=captured)
    other = NativeSubmit(command=InstructionSubmit(request_id="C6-other-CID", content="B-shaped independent request."),
                         capture=b_capture(h.case))
    responses, completions = [], []
    child_started, transport_cancelled = threading.Event(), threading.Event()

    async def model(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            completions.append(body)
            assert len(completions) == 1 and not {"task", "read_skill"} & {
                tool["function"]["name"] for tool in body["tools"]}
            return sse(request, {"content": "The other CID was not cancelled."}, 1)
        assert request.url.path.endswith("/responses")
        responses.append(body)
        if len(responses) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=response_bytes(1, tool="task", arguments=TASK))
        assert len(responses) == 2 and not body.get("tools")
        child_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            transport_cancelled.set()
            raise

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    assert h.invoke(parent).status_code == 202
    assert child_started.wait(30), (len(responses), h.errors, h.api_calls)
    active = h.call(h.runtime.read, parent.command.request_id)
    assert h.invoke(other).status_code == 202
    queued = h.call(h.runtime.read, other.command.request_id)
    assert queued.status == "accepted" and queued.conversation_id != active.conversation_id
    assert completions == []
    assert h.invoke(NativeCancel(conversation_id=active.conversation_id, request_id=active.request_id)).status_code == 200
    cancelled = h.call(h.finished, active.request_id)
    continued = h.call(h.finished, queued.request_id)
    assert cancelled.status == "unknown" and cancelled.failure_code == "NATIVE_TURN_CANCELLED"
    assert cancelled.result is None and cancelled.delivered is False
    bridge = next(binding.delegation_runtime for _, _, binding in h.bindings
                  if binding.delegation_runtime is not None)
    child = h.call(bridge.read)
    assert child.parent_request_id == cancelled.request_id and child.conversation_id == cancelled.conversation_id
    assert child.status == "cancelled" and child.cancel_requested
    assert child.stop_confirmed and child.child_cancelled and child.parent_idle
    assert transport_cancelled.is_set()
    assert cancelled.usage.state == "partial"
    assert (cancelled.usage.usage[0].requests, cancelled.usage.usage[0].measured_requests) == (2, 1)
    assert continued.status == "completed" and not continued.cancel_requested
    page = h.call(h.runtime.result, NativeResult(conversation_id=continued.conversation_id,
                                               request_id=continued.request_id))
    assert page.text == "The other CID was not cancelled." and continued.usage.usage[0].requests == 1
    assert len(h.bindings) == 3 and len(h.sdk_calls) == 2
    assert h.invoke(parent).json()["status"] == "unknown"
    assert h.call(bridge.read) == child and len(responses) == 2 and len(completions) == 1
    reports = {body["request_id"]: body["usage"] for path, body, status in h.api_calls
               if path.endswith("/turn-usage") and status == 200}
    assert reports == {cancelled.request_id: cancelled.usage.model_dump(mode="json"),
                       continued.request_id: continued.usage.model_dump(mode="json")}


@pytest.mark.skipif(sys.platform != "win32", reason="This assertion preserves the real Windows platform refusal.")
def test_exact_linux_a_profile_without_os_double_is_refused_before_binding(integrated):
    h = integrated
    command = NativeSubmit(command=InstructionSubmit(request_id="C6-wrong-platform", content=TEXT),
                           capture=a_capture(h.case))
    response = h.invoke(command)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    assert h.bindings == h.sdk_calls == h.api_calls == []


def test_followed_a_cid_restores_manual_responses_and_keeps_last_good_checkpoint_after_unknown(
        integrated, monkeypatch, tmp_path):
    from azure.ai.projects.models import VersionRefIndicator

    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    h = integrated
    state = h.invoke(NativeSessionRead()).json()
    prepared = h.invoke(NativeSessionPrepare(
        request_id="C6-prepared", expected_session_id=h.identity.session_id, expected_etag=state["etag"]))
    assert prepared.status_code == 200, prepared.text
    captured, requests = a_capture(h.case), []
    summary = SUMMARY.replace("</overview>", "</overview>\n").replace("</history>", "</history>\n")

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        count = len(requests)
        assert count <= 8 and body["model"] == "gpt-5.4-mini" and body["store"] is False
        assert "previous_response_id" not in body
        if count in {2, 5}:
            assert tool_result(body, "call_1" if count == 2 else "call_4")["text"] == SKILL_BODY
        if count in {4, 6, 8}:
            assert "inventory count 850" in json.dumps(body["input"])
        if count == 7:
            return httpx.Response(503, json={"error": {"code": "OFFLINE_UNCONFIRMED_MODEL"}})
        answers = {2: "FIRST inventory count 850.", 3: summary, 5: "SECOND inventory count 850.",
                   6: "THIRD inventory count 850.", 8: "LAST GOOD inventory count 850."}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            count, tool="read_skill" if count in {1, 4} else None,
            arguments={"name": "receipt-guide"}, text=answers.get(count),
            public_summary=summary if count == 3 else "Public offline reasoning summary.",
        ))

    transport = httpx.MockTransport(model)
    h.runtime.executor.model_transport = transport
    first_command = NativeSubmit(command=InstructionSubmit(request_id="C6-cold-1", content=TEXT), capture=captured)
    assert h.invoke(first_command).status_code == 202
    first = h.call(h.finished, first_command.command.request_id)
    assert first.status == "completed", (first.failure_code, h.errors, h.api_calls)
    old_state = h.invoke(NativeSessionRead()).json()
    h.close()

    version = str(int(h.identity.agent_version) + 1)
    next_target = h.target.model_copy(update={
        "agent_version": version,
        "session_id": native_session_id(h.target, h.case.h.config.identity.managed_identity_client_id, version=version),
    })
    identity = h.identity.model_copy(update={"session_id": next_target.session_id, "agent_version": version})
    hosted = SimpleNamespace(identity=identity, api=h.hosted.api,
                             supervisor=SimpleNamespace(journal=SimpleNamespace(root=tmp_path / "new-agent")))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    with monkeypatch.context() as environment:
        environment.setenv("FOUNDRY_HOSTING_ENVIRONMENT", "OFFLINE-next-factory")
        environment.setenv("AGENTSERVER_TASKS_BACKEND", "hosted")
        environment.setenv("BEHAVIOR_ASSISTANT_RUNTIME_PATH", str(h.runtime.executor.runtime_path))
        environment.setenv("FOUNDRY_PROJECT_ENDPOINT", MODEL_ROOT)
        runtime = native_sdk.native_runtime_from_environment(hosted)
    runtime.executor.model_transport = transport
    runtime.executor.capability_providers = tuple(h.observe_provider(provider)
                                                 for provider in runtime.executor.capability_providers)
    platform_reads = []

    def platform_http(request):
        assert request.method == "GET"
        platform_reads.append((request.url.path, dict(request.url.params)))
        if "/endpoint/sessions/" in request.url.path:
            assert request.url.path.endswith("/" + h.identity.session_id)
            return httpx.Response(200, json={"agent_session_id": h.identity.session_id, "status": "idle"})
        if request.url.path.endswith("/tasks/" + identity.agent_id):
            return httpx.Response(200, json={
                "id": identity.agent_id, "agent_name": identity.agent_name, "session_id": h.identity.session_id,
                "status": "suspended", "lease": None, "payload": {"steering": {"pending_inputs": []}},
            })
        assert request.url.path.endswith("/tasks")
        assert request.url.params["session_id"] == h.identity.session_id
        return httpx.Response(200, json={"data": [], "has_more": False})

    runtime.task_observer.transport = httpx.MockTransport(platform_http)
    runtime.bootstrap_source_inactive.transport = httpx.MockTransport(platform_http)

    class PlatformClient:
        def __init__(self, **_):
            self.agents = self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_session(self, name, sid):
            assert (name, sid) == (identity.agent_name, identity.session_id)
            return SimpleNamespace(
                agent_session_id=sid, status="active", version_indicator=VersionRefIndicator(agent_version=version))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", PlatformClient)
    deployed_versions(monkeypatch, {identity.agent_id: version})
    with TestClient(create_host(hosted, native_runtime=runtime)) as client:
        h.servers[identity.session_id] = (client, identity)

        def invoke(command):
            return client.post("/invocations", json=command.model_dump(mode="json"), headers={
                NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, "x-agent-session-id": identity.session_id,
                "x-agent-invocation-id": "offline-next-" + uuid4().hex,
                "x-agent-foundry-call-id": "C6-original-call",
            })

        async def finished(rid):
            async with asyncio.timeout(90):
                while True:
                    receipt = await runtime.read(rid)
                    if receipt.status in {"completed", "unknown"} and any(
                            path.endswith("/turn-usage") and body["request_id"] == rid and status == 200
                            for path, body, status in h.api_calls):
                        return receipt
                    await asyncio.sleep(0.02)

        follow = NativeSessionFollow(request_id="C6-original-follow", expected_session_id=h.identity.session_id,
                                     expected_etag=old_state["etag"], stage="prepare")
        pending = invoke(follow)
        assert pending.status_code == 200 and pending.json()["state"] == "pending", pending.text
        ready = invoke(follow.model_copy(update={"stage": "commit"}))
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        assert ready.json()["model_started"] is False and len(requests) == 2
        assert invoke(NativeRead(request_id=first.request_id)).json() == first.model_dump(mode="json")
        selected = native_sessions(h.case.h.composition).current(h.foundry, refresh=True)
        assert selected.target.session_id == identity.session_id
        conversation = NativeConversation.model_validate_json(
            invoke(NativeConversationRead(conversation_id=first.conversation_id)).content)
        assert conversation.hosted_session_id == h.identity.session_id
        assert conversation.execution_hosted_session_id == identity.session_id
        assert conversation.continuation_blocker is None
        completed, unknown = [], None
        for index, expected in ((2, "SECOND"), (3, "THIRD"), (4, None), (5, "LAST GOOD")):
            command = NativeSubmit(command=InstructionSubmit(
                request_id=f"C6-cold-{index}", conversation_id=first.conversation_id,
                content=f"Continue the original inventory, turn {index}."), capture=captured)
            response = invoke(command)
            assert response.status_code == 202, response.text
            receipt = client.portal.call(finished, command.command.request_id)
            assert receipt.capture == captured and receipt.conversation_id == first.conversation_id
            if expected is None:
                assert receipt.status == "unknown" and receipt.result is None
                assert receipt.usage.state == "partial" and receipt.usage.usage[0].measured_requests == 0
                unknown = receipt
                assert invoke(command).json()["status"] == "unknown" and len(requests) == 7
            else:
                assert receipt.status == "completed", (receipt.failure_code, len(requests), h.errors, h.api_calls)
                assert receipt.hosted_session_id == identity.session_id
                page = client.portal.call(runtime.result, NativeResult(
                    conversation_id=first.conversation_id, request_id=receipt.request_id))
                assert page.text == expected + " inventory count 850."
                completed.append(receipt)
        assert invoke(NativeRead(request_id=unknown.request_id)).json() == unknown.model_dump(mode="json")
        assert invoke(NativeRead(request_id=first.request_id)).json() == first.model_dump(mode="json")
    turns = [options["native_checkpoint_turn"] for options in h.sdk_calls]
    assert len(turns) == 5 and len(requests) == 8
    assert len({options["sdk_session_id"] for options in h.sdk_calls}) == 1
    assert turns[1].previous.sha256 == first.result.checkpoint_sha256
    assert turns[1].previous.lineage.request_id == first.request_id
    assert turns[2].previous.codec == "native-session-fs-responses-diag-v1"
    assert turns[2].previous.source.execution.request_id == completed[0].request_id
    assert turns[4].previous == turns[3].previous
    assert turns[4].previous.sha256 == completed[1].result.checkpoint_sha256
    assert turns[4].previous.source.execution == turns[2].previous.source.execution
    assert len({outcome["sdk_stop"]["pid"] for outcome in h.sdk_outcomes}) == 4
    assert all(outcome["sdk_stop"]["graceful"] for outcome in h.sdk_outcomes)
    assert any("/endpoint/sessions/" in path for path, _ in platform_reads)
    assert h.case.control.records == {}


@pytest.mark.parametrize("integrated", ["peers"], indirect=True)
def test_a_all_ten_capabilities_share_original_ui_turn_and_image_readback(
        integrated, monkeypatch, tmp_path, signing_key):
    from shared_context import behavior_desktop, behavior_desktop_capability, behavior_desktop_tools, mcp_client
    from shared_context import behavior_shell_capability as shell
    from shared_context.native_local_capabilities import NativeOwner
    from test_behavior_desktop import FakeRunner
    from test_capability_live_mix_native import live_like_catalog
    from test_capability_proxy_native import KEY, served
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from shared_context.native_image_wire import NativeImageContent, NativeImageRead, NativeImageReceipt, NativeImagesRead
    from shared_context.native_skill_draft_wire import NativeSkillDraftReceipt

    h = integrated
    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    desktop = FakeRunner()
    monkeypatch.setattr(behavior_desktop, "SubprocessRunner", lambda: desktop)
    monkeypatch.setattr(behavior_desktop_capability, "is_dumpable", lambda: False)
    monkeypatch.setattr(behavior_desktop_tools, "is_dumpable", lambda: False)
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBIQ_API_KEY", KEY)
    web_calls, shell_calls, shell_closed = [], [], []
    monkeypatch.setattr(mcp_client, "list_tools", served(mcp_client.list_tools, web_calls))
    monkeypatch.setattr(mcp_client, "call_tool", served(mcp_client.call_tool, web_calls))
    create_process = asyncio.create_subprocess_exec

    async def external_process(*args, **kwargs):
        if args[0] != "/C6/offline/bash":
            return await create_process(*args, **kwargs)
        shell_calls.append((args, kwargs))
        assert args == ("/C6/offline/bash", "--noprofile", "--norc", "-c", "printf 'C6 shell result\\n'")
        assert KEY not in json.dumps(kwargs["env"])
        stdout = asyncio.StreamReader()
        stdout.feed_data(b"C6 shell result\n")
        stdout.feed_eof()
        return SimpleNamespace(pid=424242, returncode=0, stdout=stdout,
                               _transport=SimpleNamespace(close=lambda: shell_closed.append(True)))

    def directories(owner):
        root = owner.root.parent
        paths = (root / "desktop" / "files", root / "shell" / "home", root / "shell" / "tmp")
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
        return paths

    def signal_group(pid, _signal):
        assert pid == 424242

    monkeypatch.setattr(shell, "prerequisites", lambda: "/C6/offline/bash")
    monkeypatch.setattr(shell, "shell_directories", directories)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", external_process)
    monkeypatch.setattr(shell, "_signal_group", signal_group)
    monkeypatch.setattr(shell.signal, "SIGKILL", 9, raising=False)
    captured = a_capture(h.case, None, catalog=live_like_catalog())
    select_profile(h.case.control, h.case.authority, h.case.human, SelectConversationProfile(
        request_id="C6-full-A-selection", expected_selection_id=None, template=captured.template))
    original_template, requests = captured.template.model_dump_json(), []
    last_observation = None
    image_receipts, draft_receipts = [], []
    rid, target = None, None
    draft_arguments = {"purpose": "Verify a retained result before reporting completion.",
                       "body": "Read the original request.\nCheck the retained receipt before reporting the result.\n"}
    content = TEXT + "\nSave the verification procedure as an unapproved skill draft, then ask the peer to check the sample."

    async def model(request):
        nonlocal last_observation
        body = json.loads(request.content)
        requests.append(body)
        count = len(requests)
        assert count <= 18 and KEY not in json.dumps(body)
        names = {tool["name"] for tool in body.get("tools", ())}
        if count == 12:
            assert names == set()
        else:
            assert {"WebIQ-MCP-web", "WebIQ-MCP-browse", "run_command", "desktop_open_file", "desktop_act",
                    "desktop_read_file", "playwright-browser_navigate", "playwright-browser_snapshot",
                    "read_skill", "stage_memory_candidate", "task", "deliver_image", "save_skill_draft",
                    "send_agent_message", "read_context"} <= names
        tool, arguments, text = None, None, None
        if count == 1:
            tool, arguments = "WebIQ-MCP-web", {"query": "C6 public sample"}
        elif count == 2:
            result = tool_result(body, "call_1")
            assert result["tool"] == "web" and result["is_error"] is False
            assert json.loads(result["text"])["webResults"][0]["content"] == "FA result for C6 public sample"
            tool, arguments = "playwright-browser_navigate", {"url": "http://127.0.0.1:9/"}
        elif count == 3:
            output = next(item["output"] for item in body["input"]
                          if item.get("type") == "function_call_output" and item["call_id"] == "call_2")
            assert "navigated:http://127.0.0.1:9/" in json.dumps(output)
            tool, arguments = "run_command", {"command": "printf 'C6 shell result\\n'"}
        elif count == 4:
            result = tool_result(body, "call_3")
            assert result == {"exit_code": 0, "output": "C6 shell result\n", "output_bytes": 16, "truncated": False}
            tool, arguments = "desktop_open_file", {"name": "notes.txt", "create": True}
        elif count in {5, 6}:
            observed = tool_result(body, f"call_{count - 1}")
            if last_observation is not None:
                assert observed["observation_id"] > last_observation
            last_observation = observed["observation_id"]
            action = ({"type": "type", "text": "C6 note"} if count == 5 else
                      {"type": "keypress", "key": "s", "modifiers": ["ctrl"]})
            tool, arguments = "desktop_act", {"observation_id": last_observation, "action": action}
        elif count == 7:
            tool, arguments = "desktop_read_file", {"name": "notes.txt"}
        elif count == 8:
            assert tool_result(body, "call_7")["text"] == "C6 note"
            tool, arguments = "stage_memory_candidate", {
                "decision": "candidate", "reason": "stable_preference", "quote": QUOTE,
            }
        elif count == 9:
            assert tool_result(body, "call_8")["effective"] is False
            tool, arguments = "read_skill", {"name": "receipt-guide"}
        elif count == 10:
            assert tool_result(body, "call_9")["text"] == SKILL_BODY
            tool = "playwright-browser_snapshot"
        elif count == 11:
            output = next(item["output"] for item in body["input"]
                          if item.get("type") == "function_call_output" and item["call_id"] == "call_10")
            assert "FA offline page" in json.dumps(output)
            tool, arguments = "task", TASK
        elif count == 12:
            text = "Child checked the public sample: 530."
        elif count == 13:
            bridge = next(binding.delegation_runtime for _, _, binding in h.bindings
                          if binding.delegation_runtime is not None)
            child = await bridge.read()
            assert child.status == "completed" and child.result == "Child checked the public sample: 530."
            tool = "read_context"
        elif count == 14:
            origin = tool_result(body, "call_13")
            assert origin["content"] == content and origin["turn_origin"]["request_id"] == rid
            assert origin["turn_origin"]["source_kind"] == "human" and origin["turn_origin"]["authorization"] is False
            tool = "playwright-browser_take_screenshot"
        elif count in {15, 16}:
            if count == 16:
                image_receipts.append(NativeImageReceipt.model_validate(tool_result(body, "call_15")))
            tool = "deliver_image"
        elif count == 17:
            image_receipts.append(NativeImageReceipt.model_validate(tool_result(body, "call_16")))
            assert image_receipts[0] == image_receipts[1]
            tool, arguments = "save_skill_draft", draft_arguments
        else:
            draft_receipts.append(NativeSkillDraftReceipt.model_validate(tool_result(body, "call_17")))
            assert draft_receipts[0].native_source.request_id == rid and draft_receipts[0].approved is False
            tool, arguments = "send_agent_message", {
                "recipient_agent_id": target.agent_id, "kind": "request", "content": "Check the public sample: 850 - 320.",
            }
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=response_bytes(count, tool=tool, arguments=arguments, text=text))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    try:
        with original_peer_transport(h, monkeypatch) as peer, original_native_console(h, tmp_path, signing_key) as console:
            target = peer.target
            ui = console.client
            draft = ui.get("/api/state").json()["composer"]
            rid = draft["request_id"]
            response = ui.post("/api/instructions/" + rid + "/submit", headers=console.headers, json={
                "content": content, "model": "gpt-5.4-mini",
                "files": [{"name": "sample.txt", "text": "Full-A user file: 850 - 320."}],
                "attachments": [{"name": "source.json", "source": h.case.control.policy.profile.source.model_dump(mode="json")}],
            })
            assert response.status_code == 200, response.text
            receipt = h.call(h.finished, rid)
            assert receipt.status == "completed", (receipt.failure_code, len(requests), h.errors, h.api_calls)
            progress = ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]})
            assert progress.status_code == 200, progress.text
            task = next(item for item in progress.json()["tasks"] if item["task_id"] == rid)
            assert task["actions"]["read-images"]["enabled"] and task["actions"]["read-skill-draft"]["enabled"]
            assert task["native_receipt"]["notification_error"] is None
            path = "/api/instructions/" + rid + "/images"
            listed = ui.get(path)
            assert listed.status_code == 200 and len(listed.json()["entries"]) == 1, listed.text
            entry = listed.json()["entries"][0]
            downloaded = ui.get(path + "/" + entry["version_id"])
            assert downloaded.status_code == 200 and downloaded.headers["content-type"] == "image/png"
            assert downloaded.headers["x-content-sha256"] == entry["sha256"]
            assert hashlib.sha256(downloaded.content).hexdigest() == image_receipts[0].sha256
            assert len(downloaded.content) == entry["byte_length"] <= 512 * 1024
            image = h.call(h.runtime.dispatch, NativeImageRead(
                conversation_id=receipt.conversation_id, request_id=rid, version_id=entry["version_id"]))
            assert isinstance(image, NativeImageContent) and image.data == downloaded.content
            assert image.submit_sha256 == receipt.submit_digest
            original_png = base64.b64decode(h.bindings[0][1].latest_image, validate=True)
            assert image.data == original_png
            saved_draft = ui.get("/api/instructions/" + rid + "/skill-draft")
            assert saved_draft.status_code == 200 and saved_draft.json()["body"] == draft_arguments["body"]
            assert saved_draft.json()["state"] == "awaiting_decision"
            received = peer.remote.records[receipt.delegation.message_id]
            assert received.capture.template.role.profile == "fa-instruction-v1"
            assert received.capture.capabilities is None and received.capture.template.skills == ()
            assert received.capture.template.settings.model_provider is None
            assert received.source.sender.request_id == rid
            repeat = NativeSubmit(command=receipt.command, capture=receipt.capture)
            assert h.invoke(repeat).json()["status"] == "completed"
            assert len(peer.remote.records) == 1
            assert h.call(h.runtime.dispatch, NativeImagesRead(
                conversation_id=receipt.conversation_id, request_id=rid)).entries[0].sha256 == entry["sha256"]
        assert receipt.status == "completed", (receipt.failure_code, len(requests), h.errors, h.api_calls)
        assert receipt.capture.template.model_dump_json() == original_template
        assert len(receipt.capture.template.settings.capabilities) == 10
        assert len(receipt.capture.capabilities.grants) == 10 and len(h.bindings) == 7
        local = next(binding for selected, _, binding in h.bindings if "playwright" in selected)
        assert local.mcp_temp_directory.is_relative_to(h.runtime.executor.root.parent)
        assert {spec.name for spec in local.tools} == {
            "run_command", "desktop_open_file", "desktop_observe", "desktop_act", "desktop_read_file",
        }
        assert all(context.receipt.request_id == receipt.request_id for _, context, _ in h.bindings)
        assert any(context.latest_image is not None for _, context, _ in h.bindings)
        assert len(shell_calls) == 1 and shell_closed == [True]
        assert Path(shell_calls[0][1]["cwd"]) == h.runtime.executor.root.parent / "desktop" / "files"
        assert web_calls == ["list_tools", "call_tool"]
        assert desktop.inputs and all(args[0] == "xdotool" for args in desktop.inputs)
        assert receipt.usage.usage[0].requests == receipt.usage.usage[0].measured_requests == 18
        assert len(requests) == 18
        assert len(shell_calls) == 1 and web_calls == ["list_tools", "call_tool"]
        assert h.sdk_calls[0]["user_text_files"][0].text == "Full-A user file: 850 - 320."
        assert h.sdk_calls[0]["attached_text"].entries[0].attachment.source == h.case.control.policy.profile.source
        assert h.sdk_calls[0]["model_capacity"] == receipt.capture.model_capacity
        page = h.call(h.runtime.result, NativeResult(conversation_id=receipt.conversation_id,
                                                   request_id=receipt.request_id))
        assert page.text.startswith("已交給 ") and "結果會在本對話另開回合回覆" in page.text
        assert not receipt.delivered and h.closed == [selected for selected, _, _ in reversed(h.bindings)]
    finally:
        behavior_desktop_capability.DESKTOP_CAPABILITY.release(NativeOwner(h.runtime.executor.root))
    assert desktop.stopped and behavior_desktop_capability.DESKTOP_CAPABILITY.current_env() is None


def test_captured_skill_change_and_foreign_scope_are_refused_without_replacing_the_original_turn(
        integrated, monkeypatch):
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context.behavior_native_capability_wire import NATIVE_CAPABILITY_PREFIX, NativeTurnScope
    from shared_context.errors import Problem
    from shared_context.native_capability_api import NativeCapabilityGateway

    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    h = integrated
    captured, requests = a_capture(h.case), []
    new_text = b"UNSELECTED_GUIDANCE_CANARY: this is a different approved version."
    command = NativeSubmit(command=InstructionSubmit(request_id="C6-fixed-source", content=TEXT), capture=captured)

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        count = len(requests)
        assert count <= 3 and new_text.decode() not in json.dumps(body)
        if count == 2:
            assert tool_result(body, "call_1")["text"] == SKILL_BODY
            original = await h.runtime.read(command.command.request_id)
            scope = {"conversation_id": original.conversation_id, "request_id": original.request_id}
            token = await h.runtime.executor.api_token_provider()
            async with httpx.AsyncClient(base_url=API_URL, headers={"Authorization": "Bearer " + token}) as api:
                for change in ({"conversation_id": "C6-other-cid"}, {"request_id": "C6-missing-rid"}):
                    denied = await api.post(NATIVE_CAPABILITY_PREFIX + "/read-skill",
                                            json={**scope, **change, "name": "receipt-guide"})
                    assert denied.status_code == 403, denied.text
                injected = await api.post(NATIVE_CAPABILITY_PREFIX + "/read-skill",
                                         json={**scope, "name": "receipt-guide", "team_id": "C6-other-team"})
                assert injected.status_code == 422
            binding = h.case.control.policy.template
            principal = BehaviorPrincipal(
                str(binding.tenant_id), str(binding.agent_oid), binding.team_id, "agent",
                h.case.authority.actors[binding.agent_oid].actor, frozenset({"read", "submit", "execute"}))
            with pytest.raises(Problem) as foreign:
                NativeCapabilityGateway(h.case.h.composition, enabled=True).skill_catalog(
                    replace(principal, team="C6-other-team"), NativeTurnScope(**scope))
            assert foreign.value.status == 403
            artifacts = h.case.authority.artifacts
            actor = h.case.authority.actors[binding.agent_oid]
            current = artifacts.get(actor, actor.team, "receipt-guide")
            sha = hashlib.sha256(new_text).hexdigest()
            uploaded = artifacts.upload(actor, actor.team, "receipt-guide", "2" * 32, current.etag,
                                        VersionInput(filename="guide.md", media_type="text/markdown", sha256=sha), new_text)
            artifacts.decide(HumanApprover(artifacts.approval.approver_oid, actor.team), actor.team, "receipt-guide",
                             uploaded.etag, ApprovalInput(decision_id="2" * 32, version_id="2" * 32,
                                                          sha256=sha, decision="approve"))
        elif count == 3:
            output = next(item["output"] for item in body["input"]
                          if item.get("type") == "function_call_output" and item["call_id"] == "call_2")
            assert "NATIVE_CAPABILITY_REFUSED" in json.dumps(output)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            count, tool="read_skill" if count < 3 else None, arguments={"name": "receipt-guide"},
            text="The original skill changed; no substituted guidance was used." if count == 3 else None,
        ))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    assert h.invoke(command).status_code == 202
    receipt = h.call(h.finished, command.command.request_id)
    assert receipt.status == "completed", (receipt.failure_code, len(requests), h.errors, h.api_calls)
    assert receipt.capture == captured and receipt.capture.template.skills[0].version_id == "1" * 32
    changed = NativeSubmit(command=command.command.model_copy(update={"content": "A different original message."}),
                           capture=captured)
    rejected = h.invoke(changed)
    assert rejected.status_code == 409 and rejected.json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert h.call(h.runtime.read, receipt.request_id) == receipt
    assert len(requests) == 3 and len(h.sdk_calls) == 1
    page = h.call(h.runtime.result, NativeResult(conversation_id=receipt.conversation_id,
                                               request_id=receipt.request_id))
    assert page.text == "The original skill changed; no substituted guidance was used."


@pytest.mark.parametrize("integrated", ["peers"], indirect=True)
def test_a_handoff_uses_original_api_and_preserves_b_zero_capability_capture(integrated, monkeypatch):
    """B's Hosted HTTP acknowledgment is external I/O; its SDK is covered separately by the B-shaped trace."""
    from shared_context import native_agent_api
    from shared_context.behavior_agent_versions import current_hosted_version
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from test_native_agent_api import AgentTransport

    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    h = integrated
    b = h.peer.b
    select_profile(b.runtime.work_policy, b.assistant.authority, h.case.human, SelectConversationProfile(
        request_id="C6-B-original-selection", expected_selection_id=None, template=b_capture(h.case).template))
    target = b.runtime.dep.native_launches.targets[0]
    version = current_hosted_version(b.runtime, target)
    target = target.model_copy(update={"agent_version": version, "session_id": native_session_id(
        target, b.config.identity.managed_identity_client_id, version=version)})
    remote = AgentTransport(target)
    remote.preparation = NativeSessionPreparation(
        agent_id=target.agent_id, request_id="C6-B-prepared", previous_hosted_session_id=None,
        hosted_session_id=target.session_id, hosted_agent_version=target.agent_version)
    native_sessions(b).prepared = remote.preparation
    foundry = FoundryHttpClient(target, Credential(), transport=httpx.MockTransport(remote))
    original_clients = b.clients
    b.clients = tuple(item for item in b.clients if not isinstance(item, FoundryHttpClient)) + (foundry,)
    repository = InMemoryRepository()
    monkeypatch.setattr(native_agent_api, "TableRepository", lambda _: repository)
    h.platform[target.session_id] = target
    captured = a_capture(h.case, (*A_SUBSET, "fa-agent-collaboration-v1"))
    requests = []

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        assert len(requests) == 1
        tools = {tool["name"]: tool for tool in body["tools"]}
        assert {"read_skill", "stage_memory_candidate", "task", "send_agent_message"} <= tools.keys()
        assert target.agent_id in tools["send_agent_message"]["parameters"]["properties"]["recipient_agent_id"]["enum"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            1, tool="send_agent_message", arguments={
                "recipient_agent_id": target.agent_id, "kind": "request", "content": "Check the public sample: 850 - 320.",
            }))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    command = NativeSubmit(command=InstructionSubmit(request_id="C6-original-handoff", content=TEXT), capture=captured)
    try:
        assert h.invoke(command).status_code == 202
        receipt = h.call(h.finished, command.command.request_id)
        assert receipt.status == "completed", (receipt.failure_code, len(requests), h.errors, h.api_calls)
        accepted = receipt.delegation
        assert accepted is not None and accepted.status == "accepted" and accepted.recipient_agent_id == target.agent_id
        assert len(remote.records) == 1
        received = remote.records[accepted.message_id]
        assert received.command.content == "Check the public sample: 850 - 320."
        assert received.source.sender.agent_id == h.identity.agent_id
        assert received.source.sender.request_id == receipt.request_id
        assert received.source.sender.conversation_id == receipt.conversation_id
        assert received.source.kind == "agent_request"
        assert received.capture.template.role.profile == "fa-instruction-v1"
        assert received.capture.template.skills == () and received.capture.capabilities is None
        assert received.capture.template.settings.model_provider is None
        page = h.call(h.runtime.result, NativeResult(conversation_id=receipt.conversation_id,
                                                   request_id=receipt.request_id))
        assert page.text.startswith("已交給 ") and "結果會在本對話另開回合回覆" in page.text
        assert receipt.delivered is False and receipt.usage.usage[0].requests == 1
        assert h.invoke(command).json()["status"] == "completed"
        assert len(requests) == len(remote.records) == 1
        assert h.case.control.records == b.runtime.work_policy.records == {}
    finally:
        b.clients = original_clients
        foundry.close()


@pytest.mark.parametrize("lose_reply", [False, True], ids=["original-reply", "lost-api-reply"])
def test_native_skillify_enters_real_m3_approval_and_c2_read_without_a_second_upload(
        integrated, monkeypatch, signing_key, jwk_client, lose_reply):
    from shared_context.native_skill_draft_wire import NativeSkillDraftReceipt, NativeSkillDraftView
    from test_m3_auth import token as signed_token
    from test_skill_purpose import signed_m3

    monkeypatch.setattr(behavior_sdk_files, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(behavior_sdk_files, "platform", SimpleNamespace(machine=lambda: "x86_64"))
    h = integrated
    captured = a_capture(h.case, (*A_SUBSET, "fa-skillify-v1"))
    original_template = captured.template.model_dump_json()
    service = h.case.authority.artifacts
    actor = h.case.authority.actors[h.case.control.policy.template.agent_oid]
    legacy_before = service.get(actor, actor.team, "receipt-guide")
    arguments = {"purpose": "Recheck the original receipt before reporting a result.",
                 "body": "Read the original request.\nVerify its saved receipt.\nReport the result and any uncertainty.\n"}
    requests, drafts, uploads = [], [], []
    lost = False
    actual_put = service.store.put

    def put(team, artifact, version, data, sha256):
        uploads.append((team, artifact, version, data, sha256))
        return actual_put(team, artifact, version, data, sha256)

    monkeypatch.setattr(service.store, "put", put)

    def receive(request, response):
        nonlocal lost
        if lose_reply and not lost and request.url.path.endswith("/skill-draft/save"):
            assert response.status_code == 200
            lost = True
            raise httpx.ReadTimeout("OFFLINE acknowledgment lost after the real M3 writer returned.", request=request)
        return response

    h.api_response[0] = receive

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        count = len(requests)
        assert count <= 6 and body["store"] is False
        tools = {tool["name"] for tool in body["tools"]}
        assert {"read_skill", "save_skill_draft", "task", "stage_memory_candidate"} <= tools
        assert "deliver_image" not in tools
        if count == 1:
            name, supplied = "read_skill", {"name": "receipt-guide"}
        elif count == 2:
            old = tool_result(body, "call_1")
            assert old["text"] == SKILL_BODY
            assert old["skill"]["source_work"] == "legacy-source" and old["skill"]["source_work_revision"] == 1
            assert not {"native_source", "native_submit_sha256"} & old["skill"].keys()
            name, supplied = "save_skill_draft", arguments
        elif count == 3:
            if lose_reply:
                output = next(item["output"] for item in body["input"]
                              if item.get("type") == "function_call_output" and item["call_id"] == "call_2")
                assert "NATIVE_SKILL_DRAFT_UNKNOWN" in json.dumps(output)
            else:
                drafts.append(NativeSkillDraftReceipt.model_validate(tool_result(body, "call_2")))
            name, supplied = "save_skill_draft", arguments
        elif count == 4:
            drafts.append(NativeSkillDraftReceipt.model_validate(tool_result(body, "call_3")))
            assert all(draft == drafts[0] for draft in drafts)
            assert drafts[-1].approved is False and drafts[-1].state_scope == "at-original-draft-write"
            name, supplied = None, None
        elif count == 5:
            assert drafts[0].artifact_id in next(tool for tool in body["tools"] if tool["name"] == "read_skill")["description"]
            name, supplied = "read_skill", {"name": drafts[0].artifact_id}
        else:
            read = tool_result(body, "call_5")
            assert read["text"] == arguments["body"]
            assert read["skill"]["native_submit_sha256"] == drafts[0].native_source.submit_sha256
            assert read["skill"]["version_id"] == drafts[0].version_id
            assert (read["skill"]["name"], read["skill"]["sha256"], read["skill"]["metadata_sha256"]) == (
                drafts[0].artifact_id, drafts[0].sha256, drafts[0].metadata_sha256)
            assert not {"source_work", "source_work_revision", "native_source"} & read["skill"].keys()
            name, supplied = None, None
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            count, tool=name, arguments=supplied,
            text="Saved an unapproved native draft." if count == 4 else
                 "Read the human-approved original native draft." if count == 6 else None,
        ))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    command = NativeSubmit(command=InstructionSubmit(
        request_id="C6-native-skill-draft", content="Create an unapproved skill draft from this procedure:\n" + arguments["body"]),
        capture=captured)
    assert h.invoke(command).status_code == 202
    original = h.call(h.finished, command.command.request_id)
    assert original.status == "completed", (original.failure_code, len(requests), h.errors, h.api_calls)
    draft = drafts[-1]
    assert len(uploads) == 1 and uploads[0][3] == arguments["body"].encode("utf-8")
    assert draft.native_source.request_id == original.request_id
    assert draft.native_source.conversation_id == original.conversation_id
    assert draft.native_source.agent_id == h.identity.agent_id
    assert draft.native_source.agent_oid == actor.oid and draft.native_source.team_id == actor.team
    assert draft.native_source.submit_sha256 == original.submit_digest
    write_calls = [(path, body) for path, body, _ in h.api_calls if "/skill-draft/" in path]
    assert [path.rsplit("/", 1)[-1] for path, _ in write_calls] == (["save", "reconcile"] if lose_reply else ["save"])
    assert all(set(body) == {"conversation_id", "request_id", "arguments"} for _, body in write_calls)
    assert original.capture.template.model_dump_json() == original_template
    assert len(original.capture.template.settings.capabilities) == 10
    native_path = ("/work-policy/v1/native-turns/conversations/" + original.conversation_id
                   + "/requests/" + original.request_id + "/skill-draft")
    owner_headers = h.case.h.headers(signing_key)

    async def read_native():
        async with httpx.AsyncClient(base_url=API_URL) as api:
            response = await api.get(native_path, headers=owner_headers)
            assert response.status_code == 200, response.text
            return NativeSkillDraftView.model_validate_json(response.content)

    before = h.call(read_native)
    assert before.state == "awaiting_decision" and before.receipt == draft and before.body == arguments["body"]
    assert h.case.authority.skills.catalog(
        actor, actor.team, (draft.artifact_id,), schema_version="fa-approved-skill-catalog-v2").entries == ()
    http, agent_headers_m3, human_headers_m3 = signed_m3(service, signing_key, jwk_client)
    auth = service.context.auth
    claims = {"aud": auth.audience, "tid": auth.tenant_id, "iss": f"https://sts.windows.net/{auth.tenant_id}/"}
    agent_headers_m3 = {"Authorization": "Bearer " + signed_token(
        signing_key, {**claims, "oid": actor.oid, "idtyp": "app"}, remove=("scp",))}
    human_headers_m3 = {"Authorization": "Bearer " + signed_token(
        signing_key, {**claims, "oid": service.approval.approver_oid,
                      "appid": service.approval.client_id, "scp": "Artifacts.Approve"})}
    path = "/teams/" + actor.team + "/artifacts/" + draft.artifact_id
    with http:
        listed = http.get("/teams/" + actor.team + "/skill-reviews", headers=human_headers_m3)
        assert listed.status_code == 200, listed.text
        entry = next(item for item in listed.json()["entries"] if item["artifact_id"] == draft.artifact_id)
        assert entry["status"] == "awaiting_decision" and entry["native_source"] == draft.native_source.model_dump(mode="json")
        record = http.get(path, headers=human_headers_m3)
        version = http.get(path + "/versions/" + draft.version_id, headers=human_headers_m3)
        assert version.status_code == 200 and version.json()["approval"] is None
        assert version.json()["native_source"] == draft.native_source.model_dump(mode="json")
        content = http.get(path + "/versions/" + draft.version_id + "/content",
                           headers={**human_headers_m3, "If-Match": version.json()["blob"]["etag"]})
        assert content.content == arguments["body"].encode("utf-8")
        decision = {"decision_id": draft.version_id, "version_id": draft.version_id, "sha256": draft.sha256,
                    "metadata_sha256": draft.metadata_sha256, "decision": "approve"}
        refused = http.post(path + "/decisions", json=decision,
                            headers={**agent_headers_m3, "If-Match": record.headers["etag"]})
        assert refused.status_code == 403
        approved = http.post(path + "/decisions", json=decision,
                             headers={**human_headers_m3, "If-Match": record.headers["etag"]})
        assert approved.status_code == 200, approved.text

    # Reconciliation and owner review must not need an invocation of the original Hosted SID.
    server = h.servers.pop(h.identity.session_id)
    try:
        after = h.call(read_native)
        assert after.state == "effective" and after.body == arguments["body"]
        assert after.receipt == draft and after.receipt.approved is False

        async def reconcile():
            token = await h.runtime.executor.api_token_provider()
            async with httpx.AsyncClient(base_url=API_URL, headers={"Authorization": "Bearer " + token}) as api:
                response = await api.post("/work-policy/v1/native-capabilities/skill-draft/reconcile", json={
                    "conversation_id": original.conversation_id, "request_id": original.request_id, "arguments": arguments})
                assert response.status_code == 200, response.text
                return NativeSkillDraftReceipt.model_validate_json(response.content)

        assert h.call(reconcile) == draft
    finally:
        h.servers[h.identity.session_id] = server
    assert len(uploads) == 1
    catalog = h.case.authority.skills.catalog(
        actor, actor.team, (draft.artifact_id,), schema_version="fa-approved-skill-catalog-v2")
    [entry] = catalog.entries
    template = captured.template.model_copy(update={"skills": (TemplateSkill(
        name=entry.name, version_id=entry.version_id, sha256=entry.sha256, metadata_sha256=entry.metadata_sha256),)})
    selected = project_capture(
        template, "C6-human-approved-native-skill", captured.model,
        head_refs=tuple(grant.ref for grant in captured.capabilities.grants), head_selection_id="C6-current-subset",
        available=h.case.h.runtime.capability_grants, count_tokens=lambda _: 0)
    reader = NativeSubmit(command=InstructionSubmit(
        request_id="C6-read-approved-native", content="Read the specific skill version the human approved."),
        capture=selected)
    assert h.invoke(reader).status_code == 202
    completed = h.call(h.finished, reader.command.request_id)
    assert completed.status == "completed", (completed.failure_code, len(requests), h.errors, h.api_calls)
    assert completed.conversation_id != original.conversation_id
    assert [original.usage.usage[0].requests, completed.usage.usage[0].requests] == [4, 2]
    assert len(requests) == 6 and len(uploads) == 1
    assert h.invoke(command).json()["status"] == "completed"
    assert h.call(h.runtime.read, original.request_id) == original
    assert service.get(actor, actor.team, "receipt-guide") == legacy_before
    assert h.case.control.records == {}


def test_native_console_reads_real_skill_draft_bytes_and_refuses_a_replaced_source(
        integrated, monkeypatch, tmp_path, signing_key):
    """A separate minimal UI configuration, not an A/B equivalence or browser-rendering claim."""
    from azure.ai.projects.models import VersionRefIndicator
    from azure.core.exceptions import ResourceNotFoundError
    from shared_context.behavior_assistant import _LocalHttp
    from shared_context.behavior_inbox import INBOX_VERSION
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from web_test_console.adapter import parse_config
    from web_test_console.bridge import create_app as console_app
    from web_test_console.instructions import InstructionConnection
    from test_conversation_profile_selection import template
    from test_web_test_console_standing import configuration

    h = integrated
    state = h.invoke(NativeSessionRead()).json()
    assert h.invoke(NativeSessionPrepare(
        request_id="C6-prepared", expected_session_id=h.identity.session_id, expected_etag=state["etag"])).status_code == 200
    grants = available_grants(None, ("fa-skillify-v1",))
    h.case.h.runtime.capability_grants = grants
    select_profile(h.case.control, h.case.authority, h.case.human, SelectConversationProfile(
        request_id="C6-dedicated-ui-selection", expected_selection_id=None,
        template=template("fa-instruction-v3", capabilities=(grants["fa-skillify-v1"].ref,))))

    class PlatformClient:
        def __init__(self, **_):
            self.agents = self

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_session(self, name, sid):
            assert name == h.identity.agent_name
            if sid != h.identity.session_id:
                raise ResourceNotFoundError("No synthetic version-specific session.")
            return SimpleNamespace(
                agent_session_id=sid, status="active",
                version_indicator=VersionRefIndicator(agent_version=h.identity.agent_version))

    monkeypatch.setattr("azure.ai.projects.AIProjectClient", PlatformClient)
    body = "Open the original receipt.\r\nReport its status without retrying.\n"
    arguments = {"purpose": "Read the original request and its saved status.", "body": body}
    requests = []

    async def model(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert len(requests) <= 2 and request.url.path.endswith("/chat/completions")
        if len(requests) == 1:
            return sse(request, {"tool_calls": [{
                "index": 0, "id": "C6-ui-original-draft", "type": "function",
                "function": {"name": "save_skill_draft", "arguments": json.dumps(arguments)},
            }]}, 1)
        return sse(request, {"content": "Saved the original unapproved draft for review."}, 2)

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    wrapper = SimpleNamespace(
        config=SimpleNamespace(channel=SimpleNamespace(version=INBOX_VERSION)),
        runtime=h.case.h.runtime, verifier=h.case.h.composition.verifier)
    # The existing console format requires a legacy bookmark; native submission must never use it.
    config = parse_config(json.dumps(configuration(wrapper, h.case.control, ids=("C6-unused-bookmark",))).encode())
    connection = InstructionConnection(
        schema_version="fa-console-instructions-connection-v1", configuration_sha256=config.sha256,
        backend_core_sha256=config.backend_core_sha256, state_profile="foundry-native-v1")  # the workbench lane
    calls, substitute = [], [False]
    with TestClient(api_application(h.case.h.composition)) as api:
        local = _LocalHttp(api)

        def forward(request):
            response = local.handle_request(request)
            calls.append((request.method, request.url.path, response.status_code))
            if substitute[0] and request.url.path.endswith("/skill-draft") and response.status_code == 200:
                value = response.json()
                value["receipt"]["native_source"]["submit_sha256"] = "0" * 64
                return httpx.Response(200, json=value, request=request)
            return response

        with httpx.Client(transport=httpx.MockTransport(forward)) as backend:
            app = console_app(
                config, tmp_path / "native-draft-console.sqlite3", backend=backend, port=8877,
                token_provider=lambda: h.case.h.headers(signing_key)["Authorization"].removeprefix("Bearer "),
                clock=lambda: h.case.h.time, environment="OFFLINE_TEST", instruction_connection=connection)
            with TestClient(app, base_url="http://127.0.0.1:8877") as ui:
                assert ui.get("/").status_code == 200
                initial = ui.get("/api/state").json()
                headers = {"Origin": "http://127.0.0.1:8877", "X-Console-CSRF": initial["csrf"]}
                assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
                draft = ui.get("/api/state").json()["composer"]
                rid = draft["request_id"]
                submit_path = "/api/instructions/" + rid + "/submit"
                accepted = ui.post(submit_path, json={
                    "content": "Create an unapproved skill draft from this procedure:\n" + body,
                    "model": "gpt-4.1-mini"}, headers=headers)
                assert accepted.status_code == 200, accepted.text
                original = h.call(h.finished, rid)
                assert original.status == "completed", (original.failure_code, h.errors, h.api_calls)
                progress = ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]})
                assert progress.status_code == 200, progress.text
                task = next(item for item in progress.json()["tasks"] if item["task_id"] == rid)
                assert task["mode"] == "native_turn" and "job_id" not in task and "binding_id" not in task
                assert task["actions"]["read-skill-draft"]["enabled"]
                path = "/api/instructions/" + rid + "/skill-draft"
                read = ui.get(path)
                assert read.status_code == 200 and read.json()["state"] == "awaiting_decision", read.text
                value = read.json()
                assert value["body"] == body
                assert value["receipt"]["native_source"]["submit_sha256"] == original.submit_digest
                downloaded = ui.get(path + ".md")
                assert downloaded.status_code == 200 and downloaded.content == body.encode("utf-8")
                assert downloaded.headers["x-content-sha256"] == hashlib.sha256(body.encode()).hexdigest()
                assert downloaded.headers["x-content-type-options"] == "nosniff"
                service = h.case.authority.artifacts
                human = HumanApprover(service.approval.approver_oid, service.approval.team)
                saved = value["receipt"]
                record = service.get(human, human.team, saved["artifact_id"])
                service.decide(human, human.team, saved["artifact_id"], record.etag, ApprovalInput(
                    decision_id=saved["version_id"], version_id=saved["version_id"], sha256=saved["sha256"],
                    metadata_sha256=saved["metadata_sha256"], decision="approve"))
                value = ui.get(path).json()
                assert value["state"] == "effective" and value["receipt"]["approved"] is False
                assert value["receipt"]["state_scope"] == "at-original-draft-write"
                before = len(calls)
                for unsafe in ({"Origin": "https://other.example"}, {"Host": "other.example"},
                               {"Sec-Fetch-Site": "cross-site"}):
                    assert ui.get(path, headers=unsafe).status_code == 403
                assert ui.get(path + "?native_source=forged").status_code == 403
                assert ui.post(submit_path, json={}, headers={"Origin": headers["Origin"]}).status_code == 403
                assert len(calls) == before
                substitute[0] = True
                refused = ui.get(path)
                assert refused.status_code == 502 and refused.json()["error"]["code"] == "NATIVE_STATE_SCOPE"
                substitute[0] = False
                assert ui.get(path).json() == value
                assert ui.get(path + ".md").content == downloaded.content
                writes = [entry for entry in h.api_calls if entry[0].endswith("/skill-draft/save")]
                assert len(writes) == 1 and len(requests) == 2
                assert h.call(h.runtime.read, rid) == original and original.delivered is False
                assert h.case.control.records == {}
                assert not any(method == "POST" and path in {"/work-policy/v1/submit", "/work-policy/v1/preflight"}
                               for method, path, _ in calls)


@pytest.mark.parametrize("integrated", ["peers"], indirect=True)
def test_saved_image_survives_original_child_cancel_and_remains_read_only_in_native_console(
        integrated, monkeypatch, tmp_path, signing_key):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError
    from shared_context import behavior_desktop, behavior_desktop_capability, behavior_desktop_tools
    from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
    from shared_context.native_image_wire import NativeImageRead, NativeImageReceipt
    from shared_context.native_local_capabilities import NativeOwner
    from test_behavior_desktop import FakeRunner
    from test_capability_live_mix_native import live_like_catalog
    from test_conversation_profile_selection import template

    h = integrated
    catalog = live_like_catalog()
    grants = available_grants(catalog, ("fa-image-delivery-v1", "fa-delegate-v1", "fa-desktop-v1"))
    selected = ("fa-delegate-v1", "fa-desktop-v1", "fa-image-delivery-v1", "playwright")
    desktop = FakeRunner()
    monkeypatch.setattr(behavior_desktop, "SubprocessRunner", lambda: desktop)
    monkeypatch.setattr(behavior_desktop_capability, "is_dumpable", lambda: False)
    monkeypatch.setattr(behavior_desktop_tools, "is_dumpable", lambda: False)
    h.case.h.runtime.capability_grants, h.case.h.runtime.capability_catalog = grants, catalog
    model_info = InstructionModel(id="gpt-5.4-mini", display_name="Offline Responses image model",
                                  supports_image_input=True)
    h.case.control.models.models[model_info.id] = model_info
    select_profile(h.case.control, h.case.authority, h.case.human, SelectConversationProfile(
        request_id="C6-image-cancel-selection", expected_selection_id=None,
        template=template("fa-instruction-v3", model_provider="openai-responses",
                          capabilities=tuple(grants[name].ref for name in selected))))
    created, requests, pictures, image_outputs = [], [], [], []
    child_started, child_cancelled = threading.Event(), threading.Event()
    actual_create = FoundryStateStore.create_item
    lost = False

    async def lost_image_ack(store, key, value, **kwargs):
        nonlocal lost
        answer = await actual_create(store, key, value, **kwargs)
        if key.startswith("img:") or key.startswith("images:"):
            created.append(key)
        if key.startswith("img:") and not lost:
            lost = True
            raise ServiceResponseError("OFFLINE original PNG write succeeded but its acknowledgment was lost.")
        return answer

    monkeypatch.setattr(FoundryStateStore, "create_item", lost_image_ack)

    async def model(request):
        body = json.loads(request.content)
        requests.append(body)
        count = len(requests)
        assert count <= 4
        if count == 4:
            assert not body.get("tools")
            child_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                child_cancelled.set()
                raise
        if count == 3:
            image_outputs.append(next(item["output"] for item in body["input"]
                                      if item.get("type") == "function_call_output" and item["call_id"] == "call_2"))
            pictures.append(NativeImageReceipt.model_validate(tool_result(body, "call_2")))
        tool = ("playwright-browser_take_screenshot", "deliver_image", "task")[count - 1]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            count, tool=tool, arguments=TASK if count == 3 else {}))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    with ExitStack() as cleanup, original_peer_transport(h, monkeypatch), original_native_console(
            h, tmp_path, signing_key) as console:
        cleanup.callback(behavior_desktop_capability.DESKTOP_CAPABILITY.release, NativeOwner(h.runtime.executor.root))
        ui = console.client
        draft = ui.get("/api/state").json()["composer"]
        rid = draft["request_id"]
        accepted = ui.post("/api/instructions/" + rid + "/submit", headers=console.headers, json={
            "content": "Save this turn's screenshot, then ask a child to compare the public sample.",
            "model": model_info.id})
        assert accepted.status_code == 200, accepted.text
        assert child_started.wait(30), (len(requests), image_outputs, h.errors, h.api_calls)
        stopped = ui.post("/api/instructions/" + rid + "/cancel", headers=console.headers, json={})
        assert stopped.status_code == 200, stopped.text
        receipt = h.call(h.finished, rid)
        assert receipt.status == "unknown" and receipt.failure_code == "NATIVE_TURN_CANCELLED"
        assert receipt.usage.state == "partial"
        assert (receipt.usage.usage[0].requests, receipt.usage.usage[0].measured_requests) == (4, 3)
        bridge = next(binding.delegation_runtime for _, _, binding in h.bindings
                      if binding.delegation_runtime is not None)
        child = h.call(bridge.read)
        assert child.status == "cancelled" and child.stop_confirmed and child.child_cancelled and child.parent_idle
        assert child.parent_request_id == rid and child_cancelled.is_set()
        progress = ui.get("/api/instructions/progress", params={"thread_id": draft["thread_id"]})
        assert progress.status_code == 200, progress.text
        task = next(item for item in progress.json()["tasks"] if item["task_id"] == rid)
        assert task["actions"]["read-images"]["enabled"] and receipt.result is None and not receipt.delivered
        before = len(console.calls)
        path = "/api/instructions/" + rid + "/images"
        listed = ui.get(path)
        assert listed.status_code == 200 and len(listed.json()["entries"]) == 1, listed.text
        entry = listed.json()["entries"][0]
        image = ui.get(path + "/" + entry["version_id"])
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert image.headers["x-content-sha256"] == hashlib.sha256(image.content).hexdigest() == pictures[0].sha256
        original = h.call(h.runtime.dispatch, NativeImageRead(
            conversation_id=receipt.conversation_id, request_id=rid, version_id=entry["version_id"]))
        assert original.data == image.content and original.submit_sha256 == receipt.submit_digest
        assert len(created) == len(set(created)) == 2 and lost
        assert all(method == "GET" for method, _, _ in console.calls[before:])
        repeat = NativeSubmit(command=receipt.command, capture=receipt.capture)
        assert h.invoke(repeat).json()["status"] == "unknown"
        assert h.call(bridge.read) == child and len(requests) == 4 and len(created) == 2
        assert ui.get(path + "/" + entry["version_id"]).content == image.content
        assert h.call(h.runtime.read, rid) == receipt
        assert h.case.control.records == {}
