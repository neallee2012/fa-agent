"""Importable deployment entry: no runpy, Azure writes or model invocation."""

import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from shared_context.errors import Problem


def test_importlib_main_keeps_explicit_profile_gate(monkeypatch):
    monkeypatch.delenv("BEHAVIOR_ASSISTANT_PROFILE", raising=False)
    module = importlib.import_module("behavior_assistant_main")
    assert module.__spec__.name == "behavior_assistant_main"
    assert module.main.__module__ == "behavior_assistant_main"
    with pytest.raises(Problem) as error:
        module.main()
    assert error.value.body["error"]["code"] == "ASSISTANT_PROFILE_DISABLED"


def test_importlib_entry_resolves_typed_sdk_and_hosted_models():
    module = importlib.import_module("behavior_assistant_main")
    wire = importlib.import_module("shared_context.behavior_assistant_wire")
    assert module.AssistantHostedRuntime.__module__ == "shared_context.behavior_assistant_hosted"
    command = wire.Save.model_validate_json(
        '{"request_id":"own-result","answer":{"item_count":3,"completed_count":2,"remaining_count":1}}'
    )
    assert command.answer.remaining_count == 1
    assert wire.Save.__pydantic_complete__



def test_cold_hosted_entry_builds_its_host_with_declared_dependency_files_only(tmp_path):
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    requirements = [
        Requirement(line) for line in (source / "requirements-hosted.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    allowed, visited = set(), set()
    while requirements:
        requirement = requirements.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        distribution = importlib.metadata.distribution(requirement.name)
        assert distribution.version in requirement.specifier
        name = canonicalize_name(requirement.name)
        if name in visited:
            continue
        visited.add(name)
        allowed.update(str(distribution.locate_file(file).resolve()) for file in distribution.files or ())
        requirements.extend(Requirement(value) for value in distribution.requires or ())

    program = """
import importlib.abc
import importlib.machinery
import json
import os
import os
from pathlib import Path
import sys
from types import SimpleNamespace

norm = lambda value: os.path.normcase(os.path.realpath(value))
allowed = {norm(value) for value in json.load(sys.stdin)}
site = norm(sys.argv[2]) + os.sep
class DeclaredDependenciesOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.origin and norm(spec.origin).startswith(site) and norm(spec.origin) not in allowed:
            raise ModuleNotFoundError("not in requirements-hosted dependency closure", name=fullname)

sys.meta_path.insert(0, DeclaredDependenciesOnly())
def offline(event, args):
    if event == "socket.connect":
        raise AssertionError("cold startup must not open network connections")
sys.addaudithook(offline)
sys.path.insert(0, sys.argv[1])
try:
    import azure.data.tables
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("API-only dependency was not blocked")

import azure.identity
import behavior_assistant_main
from shared_context import behavior_hosted, behavior_model_direct_probe
made = []
# Stand-ins for the managed identity, the API client and the model probe: they would reach the network.
azure.identity.ManagedIdentityCredential = lambda **options: made.append(("credential", options)) or "credential"
behavior_hosted.BehaviorExecutorClient = lambda *args: made.append(("api", args)) or SimpleNamespace(api_url=args[0])
behavior_model_direct_probe.start = lambda credential: made.append(("probe", credential))
app = behavior_assistant_main.create_assistant_host()
assert type(app).__name__ == "InvocationAgentServerHost", type(app)
assert made == [("credential", {"client_id": None}),
                ("api", (os.environ["BEHAVIOR_API_URL"], os.environ["BEHAVIOR_API_AUDIENCE"], "credential")),
                ("probe", "credential")], made
assert (Path(os.environ["HOME"]) / ".fa-assistant-v12").is_dir()
# The native turn and Responses modules the deployed host loads when FA_NATIVE_TURNS / FA_RESPONSES_ENABLED are set.
import shared_context.native_sdk, shared_context.native_responses
print("cold Hosted entry -> host built offline; undeclared dependency files denied")
"""
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("FA_", "FOUNDRY_", "BEHAVIOR_", "AZURE_"))}
    environment.update({
        "BEHAVIOR_ASSISTANT_PROFILE": "assistant-v1.2",
        "FOUNDRY_AGENT_ID": "11111111-1111-4111-8111-111111111111", "FOUNDRY_AGENT_NAME": "cold-agent",
        "FOUNDRY_AGENT_VERSION": "1", "FOUNDRY_AGENT_SESSION_ID": "22222222-2222-4222-8222-222222222222",
        "HOME": str(tmp_path), "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(tmp_path / "never-started"),
        "BEHAVIOR_API_URL": "https://context.invalid",
        "BEHAVIOR_API_AUDIENCE": "api://33333333-3333-4333-8333-333333333333",
    })
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "-B", "-c", program, str(source), sysconfig.get_path("purelib")],
        input=json.dumps(sorted(allowed)), capture_output=True, text=True, encoding="utf-8", timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_unreadable_private_runtime_is_a_disabled_profile(monkeypatch, tmp_path):
    from shared_context import behavior_hosted
    from shared_context.behavior_assistant_hosted import AssistantHostedRuntime

    def missing(root):
        raise FileNotFoundError(root)

    monkeypatch.setattr(behavior_hosted, "ChecksumSupervisor", missing)
    for name, value in {
        "BEHAVIOR_ASSISTANT_PROFILE": "assistant-v1.2",
        "FOUNDRY_AGENT_ID": "11111111-1111-4111-8111-111111111111", "FOUNDRY_AGENT_NAME": "cold-agent",
        "FOUNDRY_AGENT_VERSION": "1", "FOUNDRY_AGENT_SESSION_ID": "22222222-2222-4222-8222-222222222222",
        "HOME": str(tmp_path), "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(tmp_path / "never-started"),
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(Problem) as error:
        AssistantHostedRuntime.from_environment()
    assert error.value.status == 403
    assert error.value.body["error"]["code"] == "ASSISTANT_PROFILE_DISABLED"
