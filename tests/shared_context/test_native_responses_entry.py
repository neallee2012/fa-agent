"""The actual Assistant entry composes the original native runtime; no cloud, token or SDK execution."""

import os
from pathlib import Path
import socket

import pytest
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.agentserver.responses import ResponsesAgentServerHost

import behavior_assistant_main as entry
from shared_context import native_responses
from shared_context.behavior_assistant_wire import PROFILE
from shared_context.behavior_foundry_probe import PROBE_AGENT_ID, PROBE_AGENT_NAME, PROBE_PROJECT_ENDPOINT
from shared_context.errors import Problem
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from shared_context.responses_probe import PROFILE as PROBE_PROFILE
from test_native_turns import IDENTITY, local_env


@pytest.fixture
def assistant_environment(local_env, monkeypatch, tmp_path):
    home = tmp_path / "isolated-home"
    home.mkdir()
    runtime = tmp_path / "OFFLINE-runtime-not-executed"
    runtime.write_text("OFFLINE composition only", encoding="utf-8")
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        isolated = home / name.lower()
        isolated.mkdir()
        monkeypatch.setenv(name, str(isolated))
    for name in (
            "FA_NATIVE_TURNS", "FA_RESPONSES_ENABLED", "FA_N6_RESPONSES_PROBE",
            "FA_N6_RESPONSES_INTERRUPT_SESSION_ID", "FA_N0_STATE_PROBE", "FA_PRIVATE_MEMORY_STORE",
            "FA_MODEL_DIRECT_PROBE_URL", "FA_MODEL_DIRECT_PROBE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "BEHAVIOR_ASSISTANT_PROFILE": PROFILE, "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime),
        "BEHAVIOR_API_URL": "https://api.offline.invalid",
        "BEHAVIOR_API_AUDIENCE": "api://22222222-2222-2222-2222-222222222222",
        "FOUNDRY_AGENT_ID": IDENTITY.agent_id, "FOUNDRY_AGENT_NAME": IDENTITY.agent_name,
        "FOUNDRY_AGENT_VERSION": IDENTITY.agent_version, "FOUNDRY_AGENT_SESSION_ID": IDENTITY.session_id,
        "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-constructor",
        "FOUNDRY_PROJECT_ENDPOINT": "https://offline.services.ai.azure.com/api/projects/offline",
        "AGENTSERVER_TASKS_BACKEND": "hosted",
    }.items():
        monkeypatch.setenv(name, value)

    def no_network(*_args, **_kwargs):
        raise AssertionError("The production composition check must not use a network or a token endpoint")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    seen = []
    factory = native_responses.native_responses_handler_factory

    def observed(hosted, native):
        seen.append((hosted, native))
        return factory(hosted, native)

    monkeypatch.setattr(native_responses, "native_responses_handler_factory", observed)
    return seen


def test_responses_flag_alone_explicitly_refuses_the_missing_native_control_runtime(
        assistant_environment, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    with pytest.raises(Problem) as missing:
        entry.create_assistant_host()
    assert missing.value.body["error"]["code"] == "NATIVE_TURNS_DISABLED"
    assert len(assistant_environment) == 1 and assistant_environment[0][1] is None


def test_both_flags_build_the_real_original_executor_and_one_runtime(assistant_environment, monkeypatch):
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    app = entry.create_assistant_host()
    assert isinstance(app, ResponsesAgentServerHost)
    assert len(assistant_environment) == 1
    hosted, native = assistant_environment[0]
    assert isinstance(native, NativeTurnRuntime) and isinstance(native.executor, NativeSdkExecutor)
    assert native.executor.reader is native and native.identity == hosted.identity
    assert native.executor.root == hosted.supervisor.journal.root / "native-turns"
    assert Path(os.environ["AGENTSERVER_STATE_ROOT"]) == hosted.supervisor.journal.root / "agentserver"


def test_responses_off_keeps_the_existing_invocations_entry(assistant_environment, monkeypatch):
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    app = entry.create_assistant_host()
    assert isinstance(app, InvocationAgentServerHost) and not isinstance(app, ResponsesAgentServerHost)
    assert assistant_environment == []


def test_dedicated_probe_does_not_construct_a_production_native_runtime(assistant_environment, monkeypatch):
    for name, value in {
        "FA_RESPONSES_ENABLED": "1", "FA_N6_RESPONSES_PROBE": PROBE_PROFILE,
        "FA_N6_RESPONSES_INTERRUPT_SESSION_ID": IDENTITY.session_id,
        "FOUNDRY_AGENT_ID": PROBE_AGENT_ID, "FOUNDRY_AGENT_NAME": PROBE_AGENT_NAME,
        "FOUNDRY_PROJECT_ENDPOINT": PROBE_PROJECT_ENDPOINT,
    }.items():
        monkeypatch.setenv(name, value)
    app = entry.create_assistant_host()
    assert isinstance(app, ResponsesAgentServerHost)
    assert assistant_environment == []
