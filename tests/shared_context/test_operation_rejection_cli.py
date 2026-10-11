"""The operator CLI uses signed original-command endpoints, never a model."""

import importlib
import hashlib
import io
import json
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection import rejection_case, reject
from test_skill_guidance_authority import operation_path


def arguments(case, action, path):
    return [
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", "11111111-1111-1111-1111-111111111111",
        "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        action, case.turn.binding.binding_id, "--request-file", str(path),
    ]


def operator_environment(case, monkeypatch, fault=None):
    cli = importlib.import_module("shared_context.behavior_operation_cli")
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(
        token=case.h.headers()["Authorization"].removeprefix("Bearer "))
    requests = []

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        http = TestClient(case.h.app)
        http.event_hooks["request"].append(lambda request: requests.append((request.method, request.url.path)))
        original_stream = http.stream

        @contextmanager
        def stream(method, path, **kwargs):
            rejection = path.endswith("/reject")
            if rejection and fault == "before-send":
                raise httpx.ConnectError("PRIVATE_TRANSPORT_DIAGNOSTIC")
            with original_stream(method, path, **kwargs) as response:
                if rejection and fault in {"response-lost", "invalid-receipt"}:
                    response.read()
                    assert response.status_code == 200
                    if fault == "response-lost":
                        raise httpx.ReadTimeout("PRIVATE_TRANSPORT_DIAGNOSTIC")
                    yield httpx.Response(200, json={"result": None, "private": "PRIVATE_TRANSPORT_DIAGNOSTIC"})
                else:
                    yield response

        http.stream = stream
        return http

    monkeypatch.setattr(cli, "InteractiveBrowserCredential", lambda **options: credential)
    monkeypatch.setattr(cli.httpx, "Client", client)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "reject")
    return cli, credential, requests


def test_operator_rejects_once_then_reads_the_original_receipt(rejection_case, monkeypatch, tmp_path, capsys):
    c = rejection_case
    cli, credential, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "original-rejection.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    assert cli.main(arguments(c, "reject", original)) == 0
    attempt = original.with_name(original.name + ".attempt.json")
    retained = attempt.read_bytes()
    assert cli.main(arguments(c, "reject", original)) == 1
    assert cli.main(arguments(c, "reconcile", original)) == 0
    assert attempt.read_bytes() == retained
    assert requests == [
        ("GET", operation_path(c.turn)),
        ("POST", operation_path(c.turn) + "/reject"),
        ("POST", operation_path(c.turn) + "/rejection-reconcile"),
    ]
    scopes = credential.authenticate.call_args_list
    assert scopes[0].kwargs["scopes"] == [
        "api://22222222-2222-2222-2222-222222222222/Behavior.Read",
        "api://22222222-2222-2222-2222-222222222222/Behavior.Approve",
    ]
    assert scopes[1].kwargs["scopes"] == ["api://22222222-2222-2222-2222-222222222222/Behavior.Read"]
    output = capsys.readouterr()
    assert "RECONCILE_REQUIRED" in output.err and '"rejected": true' in output.out
    assert credential.get_token.return_value.token not in output.out + output.err
    assert c.effects == [] and c.h.endpoint.requests == []


@pytest.mark.parametrize("fault,expected,posts", [
    ("before-send", 1, 0), ("response-lost", 0, 1), ("invalid-receipt", 0, 1),
])
def test_uncertain_cli_response_keeps_the_attempt_and_only_reconciles(
        rejection_case, monkeypatch, tmp_path, capsys, fault, expected, posts):
    c = rejection_case
    cli, credential, requests = operator_environment(c, monkeypatch, fault)
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    assert cli.main(arguments(c, "reject", original)) == 1
    attempt = original.with_name(original.name + ".attempt.json")
    retained = attempt.read_bytes()
    assert cli.main(arguments(c, "reject", original)) == 1
    assert cli.main(arguments(c, "reconcile", original)) == expected
    assert attempt.read_bytes() == retained
    assert requests.count(("POST", operation_path(c.turn) + "/reject")) == posts
    assert requests.count(("POST", operation_path(c.turn) + "/rejection-reconcile")) == 1
    output = capsys.readouterr()
    assert ('"state": "confirmed"' in output.out) == (expected == 0)
    assert "PRIVATE_TRANSPORT_DIAGNOSTIC" not in output.out + output.err
    assert credential.get_token.return_value.token not in output.out + output.err
    assert c.effects == []


@pytest.mark.parametrize("interactive,answer", [(False, "reject"), (True, "cancel")])
def test_human_confirmation_is_required_and_never_creates_an_attempt_on_cancel(
        rejection_case, monkeypatch, tmp_path, interactive, answer):
    c = rejection_case
    cli, credential, requests = operator_environment(c, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    assert cli.main(arguments(c, "reject", original)) == 1
    assert not original.with_name(original.name + ".attempt.json").exists()
    assert requests == ([("GET", operation_path(c.turn))] if interactive else [])
    if not interactive:
        credential.authenticate.assert_not_called()


@pytest.mark.parametrize("field", ["description_digest", "adapter_contract_digest"])
def test_stale_prepared_material_cannot_be_confirmed(rejection_case, monkeypatch, tmp_path, field):
    c = rejection_case
    cli, _, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "stale.json"
    original.write_text(json.dumps({**c.command, field: "0" * 64}), encoding="utf-8")
    assert cli.main(arguments(c, "reject", original)) == 1
    assert requests == [("GET", operation_path(c.turn))]
    assert not original.with_name(original.name + ".attempt.json").exists()


@pytest.mark.parametrize("change", ["request", "api", "binding"])
def test_original_attempt_cannot_move_to_other_material_or_connection(
        rejection_case, monkeypatch, tmp_path, change):
    c = rejection_case
    cli, credential, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    assert cli.main(arguments(c, "reject", original)) == 0
    before = list(requests)
    credential.authenticate.reset_mock()
    args = arguments(c, "reconcile", original)
    if change == "request":
        original.write_text(json.dumps({**c.command, "request_id": "new-request"}), encoding="utf-8")
    elif change == "api":
        args[args.index("--api") + 1] = "https://other.example.westus3.azurecontainerapps.io"
    else:
        args[args.index("reconcile") + 1] = "another-binding"
    assert cli.main(args) == 1
    credential.authenticate.assert_not_called()
    assert requests == before


def test_racing_attempt_file_is_never_overwritten_or_sent(rejection_case, monkeypatch, tmp_path):
    c = rejection_case
    cli, _, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    attempt = original.with_name(original.name + ".attempt.json")

    def raced_confirmation(_prompt):
        attempt.write_bytes(b"another original attempt must remain")
        return "reject"

    monkeypatch.setattr("builtins.input", raced_confirmation)
    assert cli.main(arguments(c, "reject", original)) == 1
    assert attempt.read_bytes() == b"another original attempt must remain"
    assert requests == [("GET", operation_path(c.turn))]


def test_attempt_flush_failure_does_not_submit_the_decision(rejection_case, monkeypatch, tmp_path):
    c = rejection_case
    cli, _, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")

    def failed_flush(_descriptor):
        raise OSError("PRIVATE_DISK_DIAGNOSTIC")

    monkeypatch.setattr(cli.os, "fsync", failed_flush)
    assert cli.main(arguments(c, "reject", original)) == 1
    assert original.with_name(original.name + ".attempt.json").exists()
    assert requests == [("GET", operation_path(c.turn))]


@pytest.mark.parametrize("size,outcome", [(8192, 0), (8193, 1)])
def test_request_file_byte_limit_is_exact(rejection_case, monkeypatch, tmp_path, size, outcome):
    c = rejection_case
    cli, credential, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "original.json"
    raw = json.dumps(c.command).encode()
    original.write_bytes(raw + b" " * (size - len(raw)))
    assert cli.main(arguments(c, "reject", original)) == outcome
    if outcome:
        credential.authenticate.assert_not_called()
        assert requests == []


def test_link_marked_request_path_is_rejected_before_authentication(rejection_case, monkeypatch, tmp_path):
    c = rejection_case
    cli, credential, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    native = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == original or native(path))
    assert cli.main(arguments(c, "reject", original)) == 1
    credential.authenticate.assert_not_called()
    assert requests == []


def test_redirect_cannot_forward_the_delegated_credential():
    cli = importlib.import_module("shared_context.behavior_operation_cli")
    observed = []

    def redirect(request):
        observed.append(request.url.host)
        return httpx.Response(307, headers={"Location": "https://outside.invalid"},
                              json={"error": {"code": "REDIRECT_DENIED"}})

    with httpx.Client(base_url="https://original.invalid", transport=httpx.MockTransport(redirect),
                      follow_redirects=True) as http:
        with pytest.raises(Problem):
            cli._request(http, lambda: "SYNTHETIC_TOKEN", "GET", "/operation")
    assert observed == ["original.invalid"]


def test_remote_error_fields_are_not_a_channel_for_secret_values():
    cli = importlib.import_module("shared_context.behavior_operation_cli")
    secret = "OPERATOR_PRIVATE_TOKEN"
    with httpx.Client(base_url="https://original.invalid", transport=httpx.MockTransport(
        lambda request: httpx.Response(503, json={"error": {"code": secret, "message": secret}}),
    )) as http:
        with pytest.raises(Problem) as denied:
            cli._request(http, lambda: secret, "POST", "/operation/reject", {"request_id": "original"})
    assert denied.value.status == 503
    assert secret not in json.dumps(denied.value.body)


def cold_cli(script):
    root = Path(__file__).resolve().parents[2]
    environment = {name: os.environ[name] for name in ("PATH", "SystemRoot", "TEMP", "TMP") if name in os.environ}
    environment.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=str(root / "src" / "fa-shared-context-poc"))
    return subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script],
                          env=environment, capture_output=True, text=True, timeout=30)


def test_operator_cli_help_loads_without_the_hosted_sdk():
    result = cold_cli(
        "import runpy,sys; sys.modules['copilot']=None; "
        "sys.argv=['behavior_operation_cli','--help']; "
        "runpy.run_module('shared_context.behavior_operation_cli',run_name='__main__')"
    )
    assert result.returncode == 0, result.stderr
    assert "{show,state,approve,reject,revoke,reconcile}" in result.stdout


def test_saved_attempt_retains_the_complete_original_command_after_input_file_loss(
        rejection_case, monkeypatch, tmp_path):
    c = rejection_case
    cli, _, requests = operator_environment(c, monkeypatch, "response-lost")
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    assert cli.main(arguments(c, "reject", original)) == 1
    attempt = original.with_name(original.name + ".attempt.json")
    retained = attempt.read_bytes()
    original.unlink()
    assert cli.main(arguments(c, "reconcile", original)) == 0
    assert not original.exists() and attempt.read_bytes() == retained
    assert requests == [
        ("GET", operation_path(c.turn)),
        ("POST", operation_path(c.turn) + "/reject"),
        ("POST", operation_path(c.turn) + "/rejection-reconcile"),
    ]


@pytest.mark.parametrize("phase", ["authenticate", "get-token"])
def test_real_credential_error_logging_cannot_escape_to_operator_stderr(phase):
    script = r'''
import socket, sys, webbrowser
sys.modules["copilot"] = None
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import InteractiveBrowserCredential
from shared_context.behavior_operation_cli import main

def forbidden(*args, **kwargs):
    raise AssertionError("No network or browser is allowed in the authentication regression")
socket.socket.connect = forbidden
webbrowser.open = forbidden
canary = "PRIVATE_AUTH_LOG_CANARY"
if PHASE == "authenticate":
    InteractiveBrowserCredential._request_token = lambda *args, **kwargs: {
        "error": "invalid_grant", "error_description": canary,
    }
else:
    InteractiveBrowserCredential.authenticate = lambda *args, **kwargs: None
    def failed_cache(*args, **kwargs):
        raise ClientAuthenticationError(canary)
    InteractiveBrowserCredential._acquire_token_silent = failed_cache
raise SystemExit(main([
    "--api", "https://poc.example.westus3.azurecontainerapps.io",
    "--tenant", "11111111-1111-1111-1111-111111111111",
    "--audience", "api://22222222-2222-2222-2222-222222222222",
    "--client-id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    "show", "binding-one",
]))
'''
    result = cold_cli("PHASE = " + repr(phase) + "\n" + script)
    assert result.returncode == 1
    assert "OPERATION_CLI_UNCONFIRMED" in result.stderr
    assert '"type": "ClientAuthenticationError"' in result.stderr
    assert "PRIVATE_AUTH_LOG_CANARY" not in result.stdout + result.stderr
    assert "No network or browser is allowed" not in result.stderr


@pytest.mark.parametrize("namespace", ["azure.identity._internal.interactive", "msal.application"])
@pytest.mark.parametrize("fail", [False, True])
def test_auth_logging_guard_restores_existing_handlers_and_leaves_other_logs_alone(
        rejection_case, monkeypatch, namespace, fail):
    from azure.core.exceptions import ClientAuthenticationError

    c = rejection_case
    cli, credential, _ = operator_environment(c, monkeypatch)
    target, other = logging.getLogger(namespace), logging.getLogger("operator-unrelated-test")
    private, unrelated = io.StringIO(), io.StringIO()
    for logger, buffer in ((target, private), (other, unrelated)):
        monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(buffer)])
        monkeypatch.setattr(logger, "propagate", False)
        monkeypatch.setattr(logger, "disabled", False)
    roots = [logging.getLogger(name) for name in ("azure", "msal")]
    state = {logger: (logger.handlers, logger.propagate, logger.disabled) for logger in (*roots, target, other)}

    def authenticate(*args, **kwargs):
        target.warning("PRIVATE_AUTH_HANDLER_CANARY")
        other.warning("UNRELATED_LOG_PRESERVED")
        if fail:
            raise ClientAuthenticationError("PRIVATE_AUTH_EXCEPTION_CANARY")

    credential.authenticate.side_effect = authenticate
    args = arguments(c, "show", None)[:-2]
    assert cli.main(args) == (1 if fail else 0)
    assert private.getvalue() == ""
    assert "UNRELATED_LOG_PRESERVED" in unrelated.getvalue()
    assert {logger: (logger.handlers, logger.propagate, logger.disabled) for logger in state} == state


@pytest.mark.parametrize("keep_input", [True, False])
def test_legacy_hash_only_attempts_are_read_without_rewriting_or_guessing_the_command(
        rejection_case, monkeypatch, tmp_path, keep_input):
    c = rejection_case
    assert reject(c).status_code == 200
    cli, credential, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "legacy.json"
    if keep_input:
        original.write_text(json.dumps(c.command), encoding="utf-8")
    marker = {
        "schema_version": "fa-operation-rejection-cli-attempt-v1",
        "api": "https://poc.example.westus3.azurecontainerapps.io",
        "tenant": "11111111-1111-1111-1111-111111111111",
        "audience": "api://22222222-2222-2222-2222-222222222222",
        "client_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "binding_id": c.turn.binding.binding_id,
        "operation_id": c.turn.binding.operation_id,
        "command_sha256": hashlib.sha256(json.dumps(
            c.command, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    }
    attempt = original.with_name(original.name + ".attempt.json")
    retained = json.dumps(marker).encode()
    attempt.write_bytes(retained)
    assert cli.main(arguments(c, "reconcile", original)) == (0 if keep_input else 1)
    assert attempt.read_bytes() == retained
    assert requests == ([("POST", operation_path(c.turn) + "/rejection-reconcile")] if keep_input else [])
    if not keep_input:
        credential.authenticate.assert_not_called()


@pytest.mark.parametrize("change", ["command", "format"])
def test_unverifiable_recovery_material_is_rejected_before_login(
        rejection_case, monkeypatch, tmp_path, change):
    c = rejection_case
    cli, credential, requests = operator_environment(c, monkeypatch)
    original = tmp_path / "original.json"
    original.write_text(json.dumps(c.command), encoding="utf-8")
    assert cli.main(arguments(c, "reject", original)) == 0
    attempt = original.with_name(original.name + ".attempt.json")
    value = json.loads(attempt.read_text(encoding="utf-8"))
    if change == "command":
        value["command"]["request_id"] = "different-request"
    else:
        value["schema_version"] = "unknown-future-format"
    retained = json.dumps(value).encode()
    attempt.write_bytes(retained)
    original.unlink()
    before = list(requests)
    credential.authenticate.reset_mock()
    assert cli.main(arguments(c, "reconcile", original)) == 1
    credential.authenticate.assert_not_called()
    assert requests == before and attempt.read_bytes() == retained
