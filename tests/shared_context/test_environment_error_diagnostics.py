"""Public environment failures keep useful facts without paths or command text."""

from pathlib import Path
import subprocess
import traceback

import pytest
from starlette.testclient import TestClient

from shared_context import environment
from shared_context.errors import Problem
from test_environment import external_tools, private_home, request
from test_hosted import client


CANARY = "ENVIRONMENT_PRIVATE_DIAGNOSTIC_CANARY"


@pytest.mark.parametrize("failure", ["permission", "disk", "tool-exit", "tool-timeout"])
def test_environment_failure_response_omits_private_details_and_releases_the_lock(
        private_home, external_tools, monkeypatch, failure):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    import hosted_main

    hosted, credential = client(lambda _call: pytest.fail("This environment read cannot call the network"))
    seeded = hosted.dispatch(request("seed", nonce="amber-v1"))["outcome"]
    source = Path(seeded["private_root"])
    before = {path.relative_to(private_home): path.read_bytes()
              for path in private_home.rglob("*") if path.is_file()}
    payload = request("read", checkpoint_sha256=seeded["checkpoint_sha256"])
    calls = []
    monkeypatch.setattr(hosted_main, "client", hosted)
    with monkeypatch.context() as fault:
        if failure in ("permission", "disk"):
            real_open = environment.os.open

            def failed_open(path, *args, **kwargs):
                if Path(path) == source / "tool-settings.json":
                    calls.append("read-settings")
                    kind = PermissionError if failure == "permission" else OSError
                    raise kind(13 if failure == "permission" else 28, CANARY, CANARY + ".txt")
                return real_open(path, *args, **kwargs)

            fault.setattr(environment.os, "open", failed_open)
        else:
            def failed_tool(*args, **kwargs):
                calls.append("tool")
                if failure == "tool-exit":
                    raise subprocess.CalledProcessError(17, [CANARY], output=CANARY, stderr=CANARY)
                raise subprocess.TimeoutExpired([CANARY], 10, output=CANARY, stderr=CANARY)

            fault.setattr(environment.subprocess, "run", failed_tool)
        with TestClient(hosted_main.app) as invocations:
            response = invocations.post("/invocations?agent_session_id=synthetic-session-a", json=payload)

    assert response.status_code == 503
    assert len(calls) == 1 and credential.scopes == []
    assert CANARY not in response.text
    error = response.json()["error"]
    assert error["code"] == ("ENVIRONMENT_HOME_NOT_WRITABLE" if failure == "permission" else "ENVIRONMENT_IO_FAILED")
    assert error["operation"] == "read"
    assert error["errno"] == {"permission": 13, "disk": 28, "tool-exit": None, "tool-timeout": None}[failure]
    if failure == "tool-exit":
        assert error["returncode"] == 17
    assert "diagnostic" not in error and "outcome" not in response.json()
    assert {path.relative_to(private_home): path.read_bytes()
            for path in private_home.rglob("*") if path.is_file()} == before
    resumed = hosted.dispatch(payload)["outcome"]
    assert resumed["checkpoint_sha256"] == seeded["checkpoint_sha256"]
    assert resumed["files"] == seeded["files"]
    hosted.http.close()


@pytest.mark.parametrize("return_code,expected", [(17, 17), (-9, -9), (None, None), (True, None), (CANARY, None)])
def test_propagated_tool_failure_retains_only_an_integer_exit_status(
        private_home, external_tools, monkeypatch, return_code, expected):
    hosted, credential = client(lambda _call: pytest.fail("No network is allowed"))
    seeded = hosted.dispatch(request("seed", nonce="amber-v1"))["outcome"]
    original = {path.relative_to(private_home): path.read_bytes()
                for path in private_home.rglob("*") if path.is_file()}
    payload = request("read", checkpoint_sha256=seeded["checkpoint_sha256"])
    calls = []

    def failed_tool(*args, **kwargs):
        calls.append("tool")
        raise subprocess.CalledProcessError(return_code, [CANARY], output=CANARY, stderr=CANARY)

    with monkeypatch.context() as fault:
        fault.setattr(environment.subprocess, "run", failed_tool)
        with pytest.raises(Problem) as failed:
            hosted.dispatch(payload)
    assert failed.value.status == 503 and failed.value.body["error"]["code"] == "ENVIRONMENT_IO_FAILED"
    assert failed.value.body["error"]["returncode"] == expected
    assert failed.value.body["error"]["errno"] is None
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    assert CANARY not in "".join(traceback.TracebackException.from_exception(failed.value).format())
    assert calls == ["tool"] and credential.scopes == []
    assert {path.relative_to(private_home): path.read_bytes()
            for path in private_home.rglob("*") if path.is_file()} == original
    assert hosted.dispatch(payload)["outcome"]["checkpoint_sha256"] == seeded["checkpoint_sha256"]
    hosted.http.close()


def test_missing_environment_does_not_chain_a_private_home_path(private_home, monkeypatch):
    home = private_home / CANARY
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    hosted, credential = client(lambda _call: pytest.fail("A missing environment cannot call the network"))
    with pytest.raises(Problem) as failed:
        hosted.dispatch(request("read", checkpoint_sha256="0" * 64))
    assert failed.value.status == 404
    assert failed.value.body["error"]["code"] == "ENVIRONMENT_NOT_FOUND"
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    assert CANARY not in "".join(traceback.TracebackException.from_exception(failed.value).format())
    assert credential.scopes == [] and list(home.iterdir()) == []
    assert hosted.dispatch(request("inspect"))["outcome"]["seeded"] is False
    hosted.http.close()
