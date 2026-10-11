"""Real credential failure paths remain offline and emit no private diagnostics."""

import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from azure.core.exceptions import ClientAuthenticationError

from shared_context import m3_cli
from test_m3_api import client
from test_m3_artifacts import HUMAN, create, m3, upload
from test_operation_rejection_cli import cold_cli


@pytest.mark.parametrize("phase", ["authenticate", "get-token"])
def test_m3_authentication_failures_hide_sdk_logs_and_exception_text(phase):
    script = r'''
import json, socket, sys, webbrowser
sys.modules["copilot"] = None
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import InteractiveBrowserCredential
from shared_context.m3_cli import main

def forbidden(*args, **kwargs):
    raise AssertionError("The auth regression must not open a network connection or browser")
socket.socket.connect = forbidden
webbrowser.open = forbidden
calls = []
canary = "M3_PRIVATE_AUTH_DIAGNOSTIC_CANARY"
if PHASE == "authenticate":
    def refused(*args, **kwargs):
        calls.append("authenticate-token")
        return {"error": "invalid_grant", "error_description": canary}
    InteractiveBrowserCredential._request_token = refused
else:
    InteractiveBrowserCredential.authenticate = lambda *args, **kwargs: None
    def refused_cache(*args, **kwargs):
        calls.append("silent-token")
        raise ClientAuthenticationError(canary)
    InteractiveBrowserCredential._acquire_token_silent = refused_cache
result = main([
    "--api", "https://poc.example.westus3.azurecontainerapps.io",
    "--tenant", "11111111-1111-1111-1111-111111111111",
    "--audience", "api://22222222-2222-2222-2222-222222222222",
    "--client-id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    "--team", "team-one", "show", "report",
])
print(json.dumps({"calls": calls, "result": result}))
raise SystemExit(result)
'''
    result = cold_cli("PHASE = " + repr(phase) + "\n" + script)
    assert result.returncode == 1
    expected = "authenticate-token" if phase == "authenticate" else "silent-token"
    assert json.loads(result.stdout) == {"calls": [expected], "result": 1}
    assert "M3_PRIVATE_AUTH_DIAGNOSTIC_CANARY" not in result.stdout + result.stderr
    failure = json.loads(result.stderr)
    assert failure["code"] == "M3_AUTHENTICATION_UNCONFIRMED"
    assert failure["phase"] == ("authentication" if phase == "authenticate" else "metadata")


@pytest.mark.parametrize("namespace", ["azure.identity._internal.interactive", "msal.application"])
@pytest.mark.parametrize("failure", [False, True])
def test_m3_restores_logger_state_and_keeps_unrelated_logs_after_success_or_failure(
        m3, monkeypatch, capsys, caplog, namespace, failure):
    service, _, _ = m3
    create(service, "sop")
    upload(service)
    http = client(service, HUMAN)
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(token="M3_SYNTHETIC_TOKEN")
    private, unrelated = io.StringIO(), io.StringIO()
    target = logging.getLogger(namespace)
    other = logging.getLogger("m3-unrelated-test")
    for logger, output in ((target, private), (other, unrelated)):
        monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(output)])
        monkeypatch.setattr(logger, "propagate", False)
        monkeypatch.setattr(logger, "disabled", False)
    roots = [logging.getLogger(name) for name in ("azure", "msal")]
    before = {logger: (logger.handlers, logger.propagate, logger.disabled) for logger in (*roots, target, other)}
    late_name = namespace.split(".", 1)[0] + ".m3_late_" + uuid4().hex
    calls = []

    def authenticate(*args, **kwargs):
        calls.append("authenticate")
        target.warning("M3_PRIVATE_LOG_HANDLER_CANARY")
        logging.getLogger(late_name).warning("M3_PRIVATE_LATE_LOG_CANARY")
        other.warning("M3_UNRELATED_LOG_PRESERVED")
        if failure:
            raise ClientAuthenticationError("M3_PRIVATE_EXCEPTION_CANARY")

    credential.authenticate.side_effect = authenticate
    monkeypatch.setattr(m3_cli, "InteractiveBrowserCredential", lambda **_options: credential)
    monkeypatch.setattr(m3_cli.httpx, "Client", lambda **_options: http)
    outcome = m3_cli.main([
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", "11111111-1111-1111-1111-111111111111",
        "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        "--team", HUMAN.team, "show", "report",
    ])
    assert outcome == int(failure) and calls == ["authenticate"]
    assert private.getvalue() == "" and "M3_UNRELATED_LOG_PRESERVED" in unrelated.getvalue()
    assert not any(record.name == late_name for record in caplog.records)
    assert {logger: (logger.handlers, logger.propagate, logger.disabled) for logger in before} == before
    output = capsys.readouterr()
    assert "M3_PRIVATE_" not in output.out + output.err
    if failure:
        assert json.loads(output.err)["code"] == "M3_AUTHENTICATION_UNCONFIRMED"
        assert output.out == ""
    else:
        assert json.loads(output.out)["artifact_id"] == "report"


@pytest.mark.parametrize("namespace", ["azure", "msal"])
@pytest.mark.parametrize("root_interception", [False, True])
def test_cold_late_child_control_distinguishes_root_interception(namespace, root_interception):
    script = r'''
import json, logging
from shared_context.human_cli_logging import private_auth_logging

assert not logging.getLogger().handlers
root = logging.getLogger(NAMESPACE)
before = (root.handlers, root.propagate, root.disabled)
name = NAMESPACE + ".cold_late_child"
assert name not in logging.root.manager.loggerDict
with private_auth_logging():
    if not INTERCEPT:
        root.handlers = before[0]
        root.propagate = before[1]
    logging.getLogger(name).warning("M3_COLD_LATE_CHILD_CANARY")
assert (root.handlers, root.propagate, root.disabled) == before
print(json.dumps({"late_child_created": name in logging.root.manager.loggerDict,
                  "root_handlers": len(logging.getLogger().handlers)}))
'''
    result = cold_cli("NAMESPACE = " + repr(namespace) + "\nINTERCEPT = " + repr(root_interception) + "\n" + script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"late_child_created": True, "root_handlers": 0}
    if root_interception:
        assert result.stderr == ""
    else:
        assert result.stderr == "M3_COLD_LATE_CHILD_CANARY\n"
