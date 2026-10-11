"""Documented CLI entrypoints must execute without changing import behavior."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"


def run_cli(tmp_path, *arguments, entrypoint="module"):
    environment = {name: os.environ[name] for name in ("PATH", "SystemRoot", "TEMP", "TMP") if name in os.environ}
    environment.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(SOURCE))
    targets = {
        "module": ["-m", "shared_context.m3_cli"],
        "wrapper": [str(SOURCE / "m3_artifacts_cli.py")],
        "import": ["-c", (
            "import sys; sys.modules['copilot']=None; "
            "sys.argv=['import-only','not-a-valid-action']; "
            "import shared_context.m3_cli; print('M3_IMPORT_ONLY')"
        )],
    }
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", *targets[entrypoint], *arguments],
        cwd=tmp_path, env=environment, input="", capture_output=True, text=True, encoding="utf-8", timeout=30,
    )


def test_documented_module_help_reaches_the_existing_parser(tmp_path):
    result = run_cli(tmp_path, "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    assert "{list,show,download,approve,reject,deactivate}" in result.stdout
    assert result.stderr == ""


def test_documented_module_missing_configuration_is_not_a_successful_noop(tmp_path):
    result = run_cli(tmp_path, "show", "report")
    assert result.returncode == 2
    assert "configure the API URL, tenant, audience, dedicated public client ID and team" in result.stderr
    assert result.stdout == ""


def test_existing_wrapper_still_exposes_help_and_configuration_errors(tmp_path):
    help_result = run_cli(tmp_path, "--help", entrypoint="wrapper")
    assert help_result.returncode == 0
    assert "{list,show,download,approve,reject,deactivate}" in help_result.stdout
    invalid = run_cli(tmp_path, "show", "report", entrypoint="wrapper")
    assert invalid.returncode == 2
    assert "configure the API URL, tenant, audience, dedicated public client ID and team" in invalid.stderr


@pytest.mark.parametrize("entrypoint", ["module", "wrapper"])
@pytest.mark.parametrize("action", ["approve", "reject", "deactivate"])
def test_noninteractive_human_decisions_still_stop_before_login(tmp_path, entrypoint, action):
    result = run_cli(
        tmp_path,
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", "11111111-1111-1111-1111-111111111111",
        "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "--team", "team-one", action, "report",
        "--version", "1" * 32, "--sha256", "0" * 64, entrypoint=entrypoint,
    )
    assert result.returncode == 1
    assert "human decisions require an interactive terminal" in result.stderr
    assert result.stdout == ""


def test_importing_the_module_does_not_parse_arguments_or_require_the_hosted_sdk(tmp_path):
    result = run_cli(tmp_path, entrypoint="import")
    assert result.returncode == 0
    assert result.stdout == "M3_IMPORT_ONLY\n" and result.stderr == ""
