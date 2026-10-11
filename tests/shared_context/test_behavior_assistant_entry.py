"""Importable deployment entry: no runpy, Azure writes or model invocation."""

import importlib
import importlib.metadata
import json
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


def test_cold_hosted_entry_reaches_acquire_with_declared_dependency_files_only(tmp_path):
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
from pathlib import Path
import sys
from types import SimpleNamespace

norm = lambda value: os.path.normcase(os.path.realpath(value))
allowed = {norm(value) for value in json.load(sys.stdin)}
site = norm(sys.argv[3]) + os.sep
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

import httpx
from pydantic import SecretStr
import behavior_assistant_main
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSupervisor
calls = []
def send(request):
    calls.append(request.url.path)
    return httpx.Response(412)
root = Path(sys.argv[2])
supervisor = AssistantSupervisor(root, runtime_path=root / "never-started")
client = AssistantClient(
    "https://context.invalid",
    SimpleNamespace(binding_id="binding", session_id="a" * 64, resume=False,
                    task=SimpleNamespace(job_id="cold-job")),
    SecretStr("b" * 64), token_provider=lambda: "SYNTHETIC_PRIVATE",
    transport=httpx.MockTransport(send),
)
try:
    supervisor._execute(SimpleNamespace(launch_id="cold-start"), client)
finally:
    client.close()
assert calls == ["/behavior/v1/bindings/binding/assistant/sessions/" + "a" * 64 + "/acquire"]
with supervisor.journal.connect() as db:
    body, = db.execute("SELECT body FROM assistant_failures WHERE launch_id='cold-start'").fetchone()
assert json.loads(body)["phase"] == "acquire"
assert json.loads(body)["upstream_status"] == 412
print("cold Hosted entry -> engine -> acquire; undeclared dependency files denied")
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "-B", "-c", program, str(source),
         str(tmp_path), sysconfig.get_path("purelib")],
        input=json.dumps(sorted(allowed)), capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
