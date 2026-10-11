"""OFFLINE configuration/package contract; no Azure requests or app installation."""
import json
import struct
import subprocess
import sys
import shutil
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from shared_context.errors import Problem
from shared_context.teams_deployment import PersonalTeamsConfig, build_artifacts, load_config

TENANT = "00000000-0000-0000-0000-000000000013"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000012"
ADMIN = "00000000-0000-0000-0000-00000000001b"
CLIENT = "22222222-2222-2222-2222-222222222222"
PRINCIPAL = "33333333-3333-3333-3333-333333333333"
UAMI = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-faexample/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-facontext-api"


def config(**updates):
    values = {
        "uami_client_id": CLIENT, "uami_principal_id": PRINCIPAL,
        "callback_origin": "https://ca-fa-behavior-poc.example.org",
        "website_url": "https://fa.example.org/teams", "privacy_url": "https://fa.example.org/privacy",
        "terms_url": "https://fa.example.org/terms", "team_id": "team-one",
    }
    return PersonalTeamsConfig(**{**values, **updates})


def test_operator_builds_personal_only_package_and_existing_uami_parameters(tmp_path):
    folder = tmp_path / "package"
    result = build_artifacts(config(), folder)
    with zipfile.ZipFile(folder / "fa-teams-personal.zip") as package:
        assert sorted(package.namelist()) == ["color.png", "manifest.json", "outline.png"]
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["manifestVersion"] == "1.19"
        assert manifest["bots"] == [{
            "botId": CLIENT, "scopes": ["personal"], "isNotificationOnly": False,
            "supportsFiles": False, "supportsCalling": False, "supportsVideo": False,
        }]
        assert "webApplicationInfo" not in manifest
        assert "authorization" not in manifest
        assert "permissions" not in manifest
        for name, size in (("color.png", 192), ("outline.png", 32)):
            image = package.read(name)
            assert image[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", image[16:24]) == (size, size)
    parameters = json.loads((folder / "bot.parameters.json").read_text())
    assert parameters["parameters"]["existingIdentityName"]["value"] == "id-facontext-api"
    assert parameters["parameters"]["endpoint"]["value"] == "https://ca-fa-behavior-poc.example.org/behavior/v1/teams/activities"
    runtime = json.loads((folder / "personal.config.json").read_text())
    assert runtime["tenant_id"] == TENANT
    assert runtime["intended_user_oid"] == ADMIN
    assert runtime["uami_resource_id"] == UAMI
    assert runtime["native_admission_enabled"] is False
    assert runtime["route"] is None
    assert result["actual_environment"] == "NOT_RUN"


@pytest.mark.parametrize("updates", [
    {"tenant_id": CLIENT}, {"subscription_id": CLIENT}, {"resource_group": "other-rg"},
    {"intended_user_oid": PRINCIPAL}, {"uami_resource_id": UAMI.replace("id-facontext-api", "other-identity")},
    {"uami_client_id": PRINCIPAL}, {"uami_client_id": ADMIN},
    {"uami_client_id": "00000000-0000-0000-0000-000000000000"},
    {"native_admission_enabled": True}, {"route": {"conversation_id": "operator-fake-route"}},
    {"client_secret": "SYNTHETIC-CREDENTIAL"}, {"scopes": ["team", "groupChat"]},
])
def test_configuration_cannot_expand_identity_route_or_enabled_scope(updates):
    with pytest.raises((Problem, ValidationError)):
        config(**updates)


@pytest.mark.parametrize("url", [
    "http://example.org", "https://127.0.0.1", "https://[::1]", "https://169.254.169.254",
    "https://localhost", "https://host.internal", "https://host.example.org:8443",
    "https://user:secret@host.example.org", "https://host.example.org?token=secret",
    "https://host.example.org/wrong-callback", "https://host.example.org/",
])
def test_callback_requires_reviewed_https_origin_not_an_arbitrary_url(url):
    with pytest.raises(Problem):
        config(callback_origin=url)


def test_invalid_config_errors_do_not_expose_submitted_credentials(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"client_secret": "SYNTHETIC-DO-NOT-LOG"}))
    with pytest.raises(Problem) as error:
        load_config(path)
    assert "SYNTHETIC-DO-NOT-LOG" not in str(error.value.body)


def test_rebuilding_is_deterministic_and_never_overwrites_an_existing_package(tmp_path):
    first = build_artifacts(config(), tmp_path / "first")
    second = build_artifacts(config(), tmp_path / "second")
    assert first["package_sha256"] == second["package_sha256"]
    with pytest.raises(Problem):
        build_artifacts(config(), tmp_path / "first")
    assert (tmp_path / "first" / "fa-teams-personal.zip").read_bytes() == (tmp_path / "second" / "fa-teams-personal.zip").read_bytes()


def test_windows_operator_wrapper_runs_local_package_build_without_cloud(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is required for the Windows operator wrapper integration test")
    inputs = tmp_path / "input with spaces.json"
    inputs.write_text(config().model_dump_json())
    script = Path(__file__).resolve().parents[2] / "deploy" / "teams" / "Invoke-TeamsPersonal.ps1"
    result = subprocess.run([
        pwsh, "-NoProfile", "-File", str(script), "-Action", "build",
        "-Config", str(inputs), "-OutputDirectory", str(tmp_path / "package with spaces"),
        "-Python", sys.executable,
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["native_admission_enabled"] is False
    assert report["route_established"] is False
    assert report["official_schema_validated"] is False
    assert (tmp_path / "package with spaces" / "fa-teams-personal.zip").is_file()


def test_duplicate_configuration_fields_are_not_silently_reinterpreted(tmp_path):
    source = tmp_path / "duplicate.json"
    source.write_text('{"native_admission_enabled":true,"native_admission_enabled":false}')
    with pytest.raises(Problem):
        load_config(source)
