"""Bounded task-local discovery witness and FA policy probe; scripted provider only."""

import asyncio
from collections import Counter
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient, CopilotRequestHandler, RuntimeConnection, ToolSet
from copilot.client import ModelCapabilities, ModelInfo, ModelLimits, ModelSupports
from copilot.generated.rpc import PermissionDecisionReject, SkillsDiscoverRequest, SkillsGetDiscoveryPathsRequest
from fastapi.testclient import TestClient

from shared_context import behavior_assistant_hosted as hosted, m2_copilot
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_skill_prompt import SKILL_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from sdk_checkpoint_local import sse
from test_approved_skill_api import BODY, add_skill, configured
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_inventory, instruction_models
from test_instruction_prompt import submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


CANARY_NAME = "sk12-task-local-canary"
CANARY_MARKER = "SK12_NATIVE_PERSONAL_CANARY_83D4"
CANARY_TEXT = (
    "---\nname: " + CANARY_NAME + "\ndescription: Text-only task-local isolation canary.\n---\n"
    + CANARY_MARKER + "\nInert visibility fixture. Do not execute commands or contact services.\n"
)
WITNESS_URL = "https://sk12.invalid/openai/v1/chat/completions"
FA_PROVIDER_URL = "https://provider.invalid/openai/v1/chat/completions"


class ProbeBlocked(RuntimeError):
    pass


def require(value, code):
    if not value:
        raise AssertionError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def owned_path(value, root):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(root):
        raise ProbeBlocked("DISCOVERY_ROOT_OUTSIDE_TASK")
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ProbeBlocked("DISCOVERY_ROOT_REPARSE_POINT")
    if not path.resolve().is_relative_to(root):
        raise ProbeBlocked("DISCOVERY_ROOT_ESCAPE")
    return path


def child_environment(root):
    value = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT")
             if name in os.environ}
    home, temp = root / "home", root / "temp"
    value.update({
        "HOME": str(home), "USERPROFILE": str(home), "HOMEDRIVE": home.drive,
        "HOMEPATH": str(home)[len(home.drive):],
        "APPDATA": str(home / "roaming"), "LOCALAPPDATA": str(home / "local"),
        "XDG_CONFIG_HOME": str(root / "config"), "TMPDIR": str(temp), "TMP": str(temp), "TEMP": str(temp),
        "COPILOT_SKIP_CLI_DOWNLOAD": "1",
    })
    for path in (home, temp, home / "roaming", home / "local", root / "config",
                 root / "work", root / "data"):
        path.mkdir(parents=True, exist_ok=True)
    return value


class IsolationObservation:
    def __init__(self, root, *, denied_skill_probe=False):
        self.sdk_version = metadata.version("github-copilot-sdk")
        require(self.sdk_version == "1.0.13", "SDK_VERSION_DIFFERS")
        self.root, self.env = root, child_environment(root)
        self.phase = "witness"
        self.starts, self.stops, self.force_stops, self.sessions = [], [], [], []
        self.stop_without_runtime = []
        self.events, self.requests, self.path_metadata, self.policies = [], [], [], []
        self.catalogs, self.reads = [], []
        self.discovery_requests, self.retention = [], []
        self.violations, self.blocker = [], None
        self.diagnostic_hint = None
        self.termination = "FAILED"
        self.witness_source = "NOT_ESTABLISHED"
        self.completed = False
        self.bridges = []
        self.denied_skill_probe = denied_skill_probe
        self.denials = []
        self.canary_path = None

    def policy(self, options):
        return {
            "available": options["available_tools"].to_list(),
            "excluded": options["excluded_tools"].to_list(),
            "tools": [tool.name for tool in options["tools"]],
            "discovery": options["enable_config_discovery"],
            "managed": options["enable_managed_settings"],
            "mcp": options["mcp_servers"], "agents": options["custom_agents"],
            "skills": options["skill_directories"], "plugins": options["plugin_directories"],
            "instructions": options["instruction_directories"],
            "system_sha256": digest(options["system_message"]),
        }

    def observe_session(self, session):
        phase = self.phase
        self.sessions.append({"phase": phase, "sid": session.session_id})

        def event(value):
            kind = value.type.value
            if kind in {"assistant.turn_retry", "session.error"}:
                status = getattr(value.data, "status_code", None)
                diagnostic = {
                    "phase": phase, "type": kind,
                    "status_code": status if type(status) is int and 100 <= status <= 599 else None,
                }
                for name in ("reason", "error_type", "error_code", "message"):
                    text = getattr(value.data, name, None)
                    diagnostic[name + "_sha256"] = (hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
                                                    if type(text) is str else None)
                self.events.append(diagnostic)
                return
            if kind in {"skill.invoked", "newskill.invoked", "tool.execution_start", "tool.execution_complete"}:
                name = getattr(value.data, "tool_name", None)
                call_id = getattr(value.data, "tool_call_id", None)
                success = getattr(value.data, "success", None)
                error = getattr(value.data, "error", None)
                error_code = getattr(error, "code", None)
                allowed = {"skill"} if phase == "witness" else set(SKILL_CAPABILITIES)
                expected_denial_attempt = (
                    self.denied_skill_probe and phase in {"fa-create", "fa-resume"}
                    and kind == "tool.execution_start" and name == "skill"
                    and call_id == "unlisted-" + phase
                    and getattr(value.data, "arguments", None) == {"skill": CANARY_NAME}
                )
                self.events.append({"phase": phase, "type": kind,
                    "tool": name if name in allowed or expected_denial_attempt else None,
                    "tool_call_sha256": hashlib.sha256(call_id.encode("utf-8", "surrogatepass")).hexdigest()
                        if type(call_id) is str else None,
                    "success": success if type(success) is bool else None,
                    "error_present": error is not None if kind == "tool.execution_complete" else None,
                    "error_code_sha256": hashlib.sha256(error_code.encode("utf-8", "surrogatepass")).hexdigest()
                        if type(error_code) is str else None,
                    "expected_denial_attempt": expected_denial_attempt,
                })
                unexpected_success = (
                    self.denied_skill_probe and phase in {"fa-create", "fa-resume"}
                    and kind == "tool.execution_complete" and call_id == "unlisted-" + phase
                    and success is True
                )
                unexpected_tool = (
                    name is not None and name not in allowed and not expected_denial_attempt
                    or phase != "witness" and kind in {"skill.invoked", "newskill.invoked"}
                )
                if unexpected_success or unexpected_tool:
                    self.violations.append("UNLISTED_NATIVE_TOOL_SUCCEEDED" if unexpected_success
                                           else "UNEXPECTED_NATIVE_TOOL")
                    asyncio.get_running_loop().create_task(session.abort())
        session.on(event)

    def install(self, monkeypatch):
        start, stop, force = CopilotClient.start, CopilotClient.stop, CopilotClient.force_stop
        create, resume = CopilotClient.create_session, CopilotClient.resume_session
        constructor, bridge = m2_copilot.CopilotClient, hosted.AssistantSession.__init__
        catalog, read_skill = hosted.AssistantClient.skill_catalog, hosted.AssistantClient.read_skill
        started_clients = {}

        async def started(client, *args, **kwargs):
            if client not in started_clients:
                if len(started_clients) >= 3:
                    self.violations.append("RUNTIME_BUDGET_EXCEEDED")
                    raise AssertionError("RUNTIME_BUDGET_EXCEEDED")
                started_clients[client] = None
            else:
                require(started_clients[client] is not None
                        and client._cli_process is started_clients[client]
                        and client._cli_process.poll() is None, "RUNTIME_RESTART_DISALLOWED")
            result = await start(client, *args, **kwargs)
            process = client._cli_process
            require(process is not None, "OWNED_RUNTIME_MISSING")
            if started_clients[client] is None:
                started_clients[client] = process
                self.starts.append({"phase": self.phase, "runtime_ordinal": len(self.starts) + 1, "pid": process.pid})
            status = await client.get_status()
            require(status.version == "1.0.83", "RUNTIME_VERSION_DIFFERS")
            return result

        async def stopped(client, *args, **kwargs):
            process = client._cli_process
            result = await stop(client, *args, **kwargs)
            if process is None:
                self.stop_without_runtime.append(self.phase)
                return result
            require(process.poll() is not None, "OWNED_RUNTIME_NOT_STOPPED")
            self.stops.append({"phase": self.phase, "pid": process.pid, "exit_code": process.returncode})
            return result

        async def forced(client, *args, **kwargs):
            self.force_stops.append(self.phase)
            return await force(client, *args, **kwargs)

        def isolated_client(**options):
            require(options["mode"] == "empty" and options["use_logged_in_user"] is False,
                    "FA_CLIENT_POLICY_CHANGED")
            return constructor(**{**options, "env": dict(self.env),
                                  "base_directory": str(self.root / "data"),
                                  "working_directory": str(self.root / "work")})

        def task_roots(options):
            if self.phase == "witness":
                return options
            before = self.policy(options)
            altered = {**options, "working_directory": str(self.root / "work"),
                       "config_directory": str(self.root / "data")}
            forwarded = self.policy(altered)
            require(before == forwarded, "FA_POLICY_CHANGED_BY_ROOT_INJECTION")
            require(altered["on_permission_request"] is options["on_permission_request"],
                    "FA_PERMISSION_HANDLER_REPLACED")
            require(forwarded["available"] == ["custom:" + name for name in SKILL_CAPABILITIES]
                    and forwarded["excluded"] == ["builtin:*", "mcp:*"]
                    and forwarded["discovery"] is False and forwarded["managed"] is False
                    and forwarded["mcp"] == {} and all(forwarded[name] == [] for name in
                        ("agents", "skills", "plugins", "instructions")), "FA_POLICY_DIFFERS")
            self.policies.append({"phase": self.phase, **forwarded})
            return altered

        async def created(client, *args, **options):
            session = await create(client, *args, **task_roots(options))
            self.observe_session(session)
            return session

        async def resumed(client, sid, **options):
            require(options["continue_pending_work"] is False, "PENDING_WORK_REPLAY_ENABLED")
            session = await resume(client, sid, **task_roots(options))
            self.observe_session(session)
            return session

        def opened(instance, *args, **options):
            bridge(instance, *args, **options)
            self.bridges.append(instance)

        def current_catalog(client):
            result = catalog(client)
            self.catalogs.append({"phase": self.phase, "sdk_sid": client.assignment.sdk_session_id,
                "entries": [{"name": entry.name, "version": entry.version_id, "sha256": entry.sha256}
                            for entry in result.entries]})
            return result

        def current_read(client, name):
            result = read_skill(client, name)
            self.reads.append({"phase": self.phase, "sdk_sid": client.assignment.sdk_session_id,
                              "name": name, "version": result.skill.version_id,
                              "sha256": result.skill.sha256, "body_sha256": hashlib.sha256(result.text.encode()).hexdigest()})
            return result

        monkeypatch.setattr(CopilotClient, "start", started)
        monkeypatch.setattr(CopilotClient, "stop", stopped)
        monkeypatch.setattr(CopilotClient, "force_stop", forced)
        monkeypatch.setattr(CopilotClient, "create_session", created)
        monkeypatch.setattr(CopilotClient, "resume_session", resumed)
        monkeypatch.setattr(m2_copilot, "CopilotClient", isolated_client)
        monkeypatch.setattr(hosted.AssistantSession, "__init__", opened)
        monkeypatch.setattr(hosted.AssistantClient, "skill_catalog", current_catalog)
        monkeypatch.setattr(hosted.AssistantClient, "read_skill", current_read)

    def write_proof(self):
        if folder := os.environ.get("FA_SK12_ISOLATION_EVIDENCE_DIR"):
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            status = "FAILED" if self.violations else self.termination
            data = {
                "status": status, "blocker": self.blocker if status == "BLOCKED" else None,
                "diagnostic_hint": self.diagnostic_hint,
                "sdk_version": self.sdk_version, "runtime_version": "1.0.83",
                "runtime_starts": self.starts, "runtime_stops": self.stops, "force_stops": self.force_stops,
                "stop_without_runtime": self.stop_without_runtime,
                "sessions": self.sessions, "requests": self.requests, "events": self.events,
                "scenario_variant": "forced-unlisted-skill" if self.denied_skill_probe else "personal-source",
                "unlisted_tool_denials": self.denials,
                "current_catalog_observations": self.catalogs, "current_custom_reads": self.reads,
                "discovery_path_metadata": self.path_metadata, "witness_source": self.witness_source,
                "discovery_request_scope": self.discovery_requests, "retention": self.retention,
                "fa_policies": self.policies, "violations": self.violations,
                "root_injection": "test-only public client env/base/cwd and session config_directory/cwd",
                "plugin_probe": "NOT_RUN; no installation or guessed persisted plugin schema",
                "full_SK12": False, "model_quality": "NOT_RUN", "live_cloud_model": "NOT_RUN",
                "scope": "one preferred personal-copilot location only, not all ambient sources",
            }
            filename = "unlisted-skill-proof.json" if self.denied_skill_probe else "isolation-proof.json"
            with (target / filename).open("x", encoding="utf-8") as output:
                json.dump(data, output, indent=2)
                output.write("\n")


class WitnessProvider(CopilotRequestHandler):
    def __init__(self, observation):
        self.observation, self.count = observation, 0
        self.request_digests = set()

    async def open_websocket(self, context):
        self.observation.violations.append("UNEXPECTED_WEBSOCKET_TRANSPORT")
        raise ProbeBlocked("UNEXPECTED_WEBSOCKET_TRANSPORT")

    async def send_request(self, request, context):
        observation = self.observation
        if request.method != "POST" or str(request.url) != WITNESS_URL:
            observation.violations.append("UNEXPECTED_HTTP_DESTINATION")
            raise ProbeBlocked("UNEXPECTED_HTTP_DESTINATION")
        self.count += 1
        packet = json.loads(request.content)
        streaming = packet.get("stream")
        body_sha = hashlib.sha256(request.content).hexdigest()
        catalog = json.dumps(packet["tools"]) + json.dumps([
            message for message in packet["messages"] if message.get("role") == "system"
        ])
        recorded = {"phase": "witness", "ordinal": self.count,
                    "body_sha256": body_sha,
                    "requested_streaming": streaming if type(streaming) is bool else None,
                    "stream_field_present": "stream" in packet,
                    "canary_name_visible": CANARY_NAME in json.dumps(packet),
                    "catalog_name_visible": CANARY_NAME in catalog,
                    "canary_body_visible": CANARY_MARKER in json.dumps(packet)}
        observation.requests.append(recorded)
        if streaming is not None and type(streaming) is not bool:
            observation.diagnostic_hint = "WITNESS_STREAM_SHAPE_UNSUPPORTED"
            raise ProbeBlocked(observation.diagnostic_hint)
        if body_sha in self.request_digests:
            recorded["classification"] = "retry_same_digest"
            observation.diagnostic_hint = "REPEATED_WITNESS_REQUEST"
            raise ProbeBlocked(observation.diagnostic_hint)
        self.request_digests.add(body_sha)
        recorded["classification"] = "new_request_digest"
        require(len(self.request_digests) <= 2, "WITNESS_REQUEST_BUDGET_EXCEEDED")

        def response(delta):
            try:
                if streaming is True:
                    result = sse(request, delta, len(self.request_digests))
                else:
                    message = {"role": "assistant", **delta}
                    if "tool_calls" in delta:
                        message["content"] = None
                        message["tool_calls"] = [
                            {name: call[name] for name in ("id", "type", "function")}
                            for call in delta["tool_calls"]
                        ]
                    result = httpx.Response(200, request=request, json={
                        "id": f"offline-{len(self.request_digests)}",
                        "object": "chat.completion", "created": 1, "model": "gpt-4.1-mini",
                        "choices": [{"index": 0, "message": message,
                                     "finish_reason": "tool_calls" if "tool_calls" in delta else "stop"}],
                        "usage": {"prompt_tokens": 1500, "completion_tokens": 100, "total_tokens": 1600},
                    })
            except Exception as error:
                recorded["response_error_type"] = type(error).__name__
                raise
            recorded.update(response_status=result.status_code,
                            response_content_type=result.headers.get("content-type"),
                            response_sha256=hashlib.sha256(result.content).hexdigest())
            return result

        tool = next((item["function"] for item in packet["tools"] if item["function"]["name"] == "skill"), None)
        if len(self.request_digests) == 1:
            schema = None if tool is None else tool.get("parameters")
            recorded["skill_schema_sha256"] = None if schema is None else digest(schema)
            if not isinstance(schema, dict) or "skill" not in schema.get("properties", {}):
                observation.diagnostic_hint = "NATIVE_SKILL_SCHEMA_NOT_ESTABLISHED"
                return response({"content": "The probe cannot invoke an unsupported skill schema."})
            require(CANARY_MARKER not in json.dumps(packet), "WITNESS_PROGRESSIVE_BODY_DIFFERS")
            delta = {"tool_calls": [{"index": 0, "id": "sk12-witness-read", "type": "function",
                                     "function": {"name": "skill", "arguments": json.dumps({"skill": CANARY_NAME})}}]}
        else:
            if CANARY_MARKER not in json.dumps(packet):
                observation.diagnostic_hint = "NATIVE_CANARY_LOAD_NOT_ESTABLISHED"
                return response({"content": "The probe did not establish the native skill body."})
            delta = {"content": "The inert isolation witness was observed."}
        return response(delta)


async def establish_witness(observation, runtime):
    provider = WitnessProvider(observation)
    client = CopilotClient(
        connection=RuntimeConnection.for_stdio(path=str(runtime)),
        mode="empty", use_logged_in_user=False, log_level="error",
        env=dict(observation.env), base_directory=str(observation.root / "data"),
        working_directory=str(observation.root / "work"), request_handler=provider,
        on_list_models=lambda: [ModelInfo("gpt-4.1-mini", "gpt-4.1-mini",
            ModelCapabilities(ModelSupports(vision=False), ModelLimits()))],
    )
    session = None
    try:
        await client.start()
        try:
            observation.discovery_requests.append({"project_paths": [], "exclude_host_skills": False})
            locations = await client.rpc.skills.get_discovery_paths(
                SkillsGetDiscoveryPathsRequest(project_paths=[], exclude_host_skills=False), timeout=15)
        except Exception as error:
            if getattr(error, "code", None) == -32601:
                raise ProbeBlocked("PUBLIC_DISCOVERY_PATHS_UNAVAILABLE") from None
            raise
        candidates = []
        for entry in locations.paths:
            checked = {"scope": entry.scope.value, "preferred": entry.preferred_for_creation,
                       "path_sha256": hashlib.sha256(entry.path.encode()).hexdigest(), "within_task": False}
            observation.path_metadata.append(checked)
            path = owned_path(entry.path, observation.root)
            checked.update(within_task=True, relative_path=str(path.relative_to(observation.root)))
            if entry.scope.value == "personal-copilot" and entry.preferred_for_creation:
                candidates.append(path)
        if len(candidates) != 1:
            raise ProbeBlocked("UNIQUE_CANONICAL_PERSONAL_ROOT_NOT_ESTABLISHED")
        directory = candidates[0] / CANARY_NAME
        directory.mkdir(parents=True, exist_ok=False)
        with (directory / "SKILL.md").open("w", encoding="utf-8", newline="\n") as output:
            output.write(CANARY_TEXT)
        discovered = await client.rpc.skills.discover(
            SkillsDiscoverRequest(project_paths=[], exclude_host_skills=False), timeout=15)
        found = [entry for entry in discovered.skills if entry.name == CANARY_NAME]
        if len(found) != 1 or found[0].source.value != "personal-copilot" or not found[0].enabled:
            raise ProbeBlocked("AMBIENT_CANARY_DISCOVERY_NOT_ESTABLISHED")
        require(found[0].path is not None, "DISCOVERED_CANARY_PATH_MISSING")
        require(owned_path(found[0].path, observation.root) == directory / "SKILL.md",
                "DISCOVERY_TARGET_DIFFERS")
        observation.canary_path = directory / "SKILL.md"
        observation.witness_source = "personal-copilot; public discovery without explicit skill_directories"
        session = await client.create_session(
            session_id="sk12-witness", model="gpt-4.1-mini",
            working_directory=str(observation.root / "work"),
            config_directory=str(observation.root / "data"),
            available_tools=ToolSet().add_builtin("skill"),
            on_permission_request=lambda request, invocation: PermissionDecisionReject(),
            system_message={"mode": "replace", "content": "Use only the inert skill witness."},
            provider={"type": "openai", "wire_api": "completions", "transport": "http",
                      "base_url": WITNESS_URL.removesuffix("/chat/completions"),
                      "model_id": "gpt-4.1-mini", "wire_model": "gpt-4.1-mini",
                      "bearer_token_provider": lambda _: "OFFLINE_SK12_WITNESS"},
            enable_config_discovery=True, enable_skills=True, enable_managed_settings=False,
            enable_file_hooks=False, enable_host_git_operations=False,
            enable_on_demand_instruction_discovery=False,
            custom_agents=[], mcp_servers={}, plugin_directories=[], instruction_directories=[],
        )
        await session.send_and_wait("Read the sk12-task-local-canary skill, then acknowledge only this fixture.", timeout=45)
        require(not observation.violations, "WITNESS_SAFETY_VIOLATION")
        if observation.diagnostic_hint:
            raise ProbeBlocked(observation.diagnostic_hint)
        require(provider.count == 2, "WITNESS_INCOMPLETE")
    finally:
        try:
            if session is not None:
                await session.disconnect()
        finally:
            if client._cli_process is not None:
                await client.stop()
    require(any(event["phase"] == "witness" and event["type"] == "skill.invoked"
                for event in observation.events), "WITNESS_NATIVE_INVOCATION_NOT_OBSERVED")


@pytest.mark.skipif(os.environ.get("FA_SK12_NATIVE_SCOPE") != "approved",
                    reason="Native SK12 needs an explicitly approved bounded runtime scope; this flag is not approval.")
def test_task_local_personal_canary_is_visible_then_excluded_from_fa_create_and_cold_resume(
    assistant, runtime_path, monkeypatch, tmp_path_factory,
):
    exercise_task_local_isolation(assistant, runtime_path, monkeypatch, tmp_path_factory, denied_skill_probe=False)


@pytest.mark.skipif(os.environ.get("FA_SK12_NATIVE_SCOPE") != "approved",
                    reason="Native SK12 needs an explicitly approved bounded runtime scope; this flag is not approval.")
def test_fa_denies_a_forced_unlisted_native_skill_call_and_keeps_current_reader_on_cold_resume(
    assistant, runtime_path, monkeypatch, tmp_path_factory,
):
    exercise_task_local_isolation(assistant, runtime_path, monkeypatch, tmp_path_factory, denied_skill_probe=True)


def exercise_task_local_isolation(assistant, runtime_path, monkeypatch, tmp_path_factory, *, denied_skill_probe):
    root = tmp_path_factory.mktemp("sk12").resolve()
    observation = IsolationObservation(root, denied_skill_probe=denied_skill_probe)
    observation.install(monkeypatch)
    try:
        asyncio.run(establish_witness(observation, runtime_path))
        add_skill(assistant)
        h, control, human = configured(assistant)
        source_body_sha = hashlib.sha256(BODY.encode()).hexdigest()
        requests = Counter()
        expected_requests = 3 if denied_skill_probe else 2

        def provider(request):
            if request.method != "POST" or str(request.url) != FA_PROVIDER_URL:
                observation.violations.append("UNEXPECTED_FA_PROVIDER_DESTINATION")
                raise ProbeBlocked("UNEXPECTED_FA_PROVIDER_DESTINATION")
            phase = observation.phase
            requests[phase] += 1
            require(requests[phase] <= expected_requests, "FA_REQUEST_BUDGET_EXCEEDED")
            packet = json.loads(request.content)
            raw = json.dumps(packet)
            catalog = json.dumps(packet["tools"]) + json.dumps([
                message for message in packet["messages"] if message.get("role") == "system"
            ])
            recorded = {"phase": phase, "ordinal": requests[phase],
                        "body_sha256": hashlib.sha256(request.content).hexdigest(),
                        "ambient_marker_visible": CANARY_NAME in raw or CANARY_MARKER in raw,
                        "ambient_body_visible": CANARY_MARKER in raw,
                        "ambient_catalog_name_visible": CANARY_NAME in catalog,
                        "unlisted_result_seen": False,
                        "current_approved_read_verified": False}
            observation.requests.append(recorded)
            leaked = (recorded["ambient_body_visible"] or recorded["ambient_catalog_name_visible"]
                      if denied_skill_probe else recorded["ambient_marker_visible"])
            if leaked:
                observation.violations.append("FA_AMBIENT_CANARY_EXPOSED")
                raise AssertionError("FA_AMBIENT_CANARY_EXPOSED")
            tools = {tool["function"]["name"] for tool in packet["tools"]}
            require(tools == set(SKILL_CAPABILITIES), "FA_MODEL_TOOL_SET_DIFFERS")
            call_id = "read-" + phase
            read_request = 2 if denied_skill_probe else 1
            if denied_skill_probe and requests[phase] == 1:
                delta = {"tool_calls": [{"index": 0, "id": "unlisted-" + phase, "type": "function",
                    "function": {"name": "skill", "arguments": json.dumps({"skill": CANARY_NAME})}}]}
            elif requests[phase] == read_request:
                if denied_skill_probe:
                    replies = [message for message in packet["messages"]
                               if message.get("role") == "tool" and message.get("tool_call_id") == "unlisted-" + phase]
                    if len(replies) != 1:
                        raise ProbeBlocked("UNLISTED_TOOL_RESULT_NOT_ESTABLISHED")
                    recorded["unlisted_result_seen"] = True
                delta = {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                    "function": {"name": "read_skill", "arguments": '{"name":"receipt-guide"}'}}]}
            else:
                results = [message for message in packet["messages"]
                           if message.get("role") == "tool" and message.get("tool_call_id") == call_id]
                require(len(results) == 1, "CURRENT_CUSTOM_READ_RESULT_MISSING")
                content = results[0]["content"]
                text = content if isinstance(content, str) else "".join(part["text"] for part in content)
                result = json.loads(text)
                require(result["text"] == BODY and result["skill"]["sha256"] == source_body_sha,
                        "CURRENT_APPROVED_SKILL_DIFFERS")
                recorded["current_approved_read_verified"] = True
                delta = {"content": "The currently approved receipt guide was read."}
            return sse(request, delta, sum(requests.values()))

        gateway = AssistantProvider(FA_PROVIDER_URL,
            SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SK12_FA")),
            transport=httpx.MockTransport(provider))
        h.client.close()
        h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
        install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
            installation=lambda: None, require_current_native_evidence=lambda: None))
        h.client = TestClient(h.app)
        h.executor.http_client, h.executor.model_transport = h.client, None
        h.executor.root = root / "fa"
        conversation = None
        prior_events, archives, costs = [], {}, {}
        original_reply = None
        user_texts = ("Read current approved guide in this conversation.",
                      "Read it again using the current catalog.")
        for index, phase in enumerate(("fa-create", "fa-resume")):
            observation.phase = phase
            canary_path = owned_path(observation.canary_path, root)
            canary_sha = hashlib.sha256(canary_path.read_bytes()).hexdigest()
            require(canary_sha == hashlib.sha256(CANARY_TEXT.encode()).hexdigest(), "AMBIENT_CANARY_CHANGED")
            accepted, command = submit(h, "sk12-" + phase, user_texts[index], conversation)
            conversation = accepted["conversation_id"]
            binding = h.runtime.bindings[accepted["binding_id"]]
            done = h.runtime.controller.step(binding.binding_id)
            require(done.state == "succeeded" and done.result_forwarded and not done.obligation_fulfilled,
                    "FA_TURN_NOT_SAVED")
            bridge = observation.bridges[-1]
            records = events_from(bridge._session_files.snapshot().files)
            require([record["data"]["content"] for record in records if record["type"] == "user.message"]
                    == list(user_texts[:index + 1]), "ORIGINAL_USER_EVENTS_CHANGED")
            public = [record for record in records if record["type"] in {"user.message", "tool.execution_complete"}]
            hashes = [digest(record) for record in public]
            require(hashes[:len(prior_events)] == prior_events, "PRIOR_LEGAL_HISTORY_CHANGED")
            prior_events = hashes
            require(not any(record["type"] in {"skill.invoked", "newskill.invoked"} for record in records),
                    "FA_NATIVE_SKILL_INVOKED")
            archive_key = key("text-archive", binding.job_id)
            require(h.store.read_snapshot(tuple(archives)) == archives, "ORIGINAL_ARCHIVE_CHANGED")
            archives[archive_key] = h.store.read_snapshot((archive_key,))[archive_key]
            inventory = instruction_inventory(h)
            require(all(inventory.rows[name] == record for name, record in costs.items()), "ORIGINAL_COST_CHANGED")
            costs.update({name: record for name, record in inventory.rows.items()
                          if record.payload["data"].get("kind") == "instruction_model_cost"})
            reply = h.client.get(f"/work-policy/v1/conversations/{conversation}/turns/1/reply", headers=h.headers())
            require(reply.status_code == 200, "ORIGINAL_REPLY_UNAVAILABLE")
            if original_reply is None:
                original_reply = reply.content
            require(reply.content == original_reply, "ORIGINAL_REPLY_CHANGED")
            require(bridge._sdk_stop.graceful, "FA_STOP_NOT_VERIFIED")
            require(hashlib.sha256(owned_path(canary_path, root).read_bytes()).hexdigest() == canary_sha,
                    "AMBIENT_CANARY_CHANGED")
            if denied_skill_probe:
                call_sha = hashlib.sha256(("unlisted-" + phase).encode()).hexdigest()
                completions = [event for event in observation.events if event["phase"] == phase
                               and event["type"] == "tool.execution_complete"
                               and event["tool_call_sha256"] == call_sha]
                if len(completions) != 1 or completions[0]["success"] is not False or not completions[0]["error_present"]:
                    raise ProbeBlocked("UNLISTED_TOOL_FAILURE_RECEIPT_NOT_ESTABLISHED")
                observation.denials.append({"phase": phase, "tool_call_sha256": call_sha,
                    "reported_success": False, "error_present": True, "canary_file_sha256": canary_sha})
            observation.retention.append({
                "phase": phase, "sdk_sid": bridge.client.assignment.sdk_session_id,
                "user_sha256": [hashlib.sha256(text.encode()).hexdigest() for text in user_texts[:index + 1]],
                "public_event_sha256": hashes,
                "original_archive_metadata_sha256": {name: digest(row.payload) for name, row in archives.items()},
                "model_cost_metadata_sha256": {name: digest(row.payload) for name, row in costs.items()},
                "first_reply_sha256": hashlib.sha256(reply.content).hexdigest(),
                "acknowledged": done.obligation_fulfilled,
                "canary_file_sha256": canary_sha,
            })
        fa_sessions = [value["sid"] for value in observation.sessions if value["phase"] != "witness"]
        require(len(fa_sessions) == 2 and fa_sessions[0] == fa_sessions[1], "COLD_RESUME_ID_DIFFERS")
        require(len(observation.starts) == len(observation.stops) == 3
                and [value["phase"] for value in observation.starts] == ["witness", "fa-create", "fa-resume"],
                "THREE_OWNED_RUNTIMES_NOT_ESTABLISHED")
        require(not observation.force_stops and not observation.violations, "SAFETY_BOUNDARY_FAILED")
        require(requests == {"fa-create": expected_requests, "fa-resume": expected_requests}, "FA_REQUEST_COUNT_DIFFERS")
        require(len(observation.bridges) == 2, "UNEXPECTED_FA_EXECUTION")
        require(len(observation.catalogs) == len(observation.reads) == 2
                and all(item["entries"] == [{"name": "receipt-guide", "version": "1" * 32,
                                             "sha256": source_body_sha}] for item in observation.catalogs)
                and all(item["name"] == "receipt-guide" and item["version"] == "1" * 32
                        and item["sha256"] == item["body_sha256"] == source_body_sha
                        for item in observation.reads), "CURRENT_CATALOG_OR_READER_NOT_ESTABLISHED")
        h.client.close()
    except ProbeBlocked as error:
        observation.completed = False
        if observation.violations:
            observation.blocker = None
            observation.termination = "FAILED"
            raise AssertionError("SK12_SAFETY_VIOLATION") from None
        observation.blocker = str(error)
        observation.termination = "BLOCKED"
        pytest.skip("SK12_BLOCKED:" + str(error))
    except BaseException:
        observation.completed = False
        observation.blocker = None
        observation.termination = "FAILED"
        raise
    else:
        observation.completed = True
        observation.termination = "SUBSET_PASS"
    finally:
        observation.write_proof()
