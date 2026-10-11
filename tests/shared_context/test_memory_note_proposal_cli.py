"""Interactive exact sharing with a durable original-command marker; no live login."""

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import TENANT
from shared_context import behavior_memory_cli as cli
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import CLIENT_ID, jwk_client, signing_key
from test_memory_note_proposals import install_case, proposal_case


def arguments(case, action, request_file=None, request_id=None):
    values = [
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", TENANT, "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", CLIENT_ID, action, case.binding.binding_id,
    ]
    return values + ([request_id] if action == "proposal" else ["--request-file", str(request_file)])


def environment(case, monkeypatch, *, lose_response=False):
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(
        token=case.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    requests = []

    def client(**options):
        http = TestClient(case.h.app)
        http.event_hooks["request"].append(lambda req: requests.append((req.method, req.url.path)))
        original = http.stream

        @contextmanager
        def stream(method, path, **kwargs):
            with original(method, path, **kwargs) as response:
                if lose_response and method == "POST" and path.endswith("/note-proposals"):
                    response.read()
                    assert response.status_code == 200
                    raise httpx.ReadTimeout("PRIVATE_PROPOSAL_TRANSPORT")
                yield response

        http.stream = stream
        return http

    monkeypatch.setattr(cli, "InteractiveBrowserCredential", lambda **_: credential)
    monkeypatch.setattr(cli.httpx, "Client", client)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "share-note")
    return credential, requests


@pytest.mark.parametrize("lose_response", [False, True])
def test_cli_preserves_original_pending_proposal_and_only_reconciles(
        proposal_case, monkeypatch, tmp_path, capsys, lose_response):
    c = proposal_case
    path, command = install_case(c)
    credential, requests = environment(c, monkeypatch, lose_response=lose_response)
    original = tmp_path / "proposal.json"
    original.write_text(json.dumps(command), encoding="utf-8")
    assert cli.main(arguments(c, "propose", original)) == (1 if lose_response else 0)
    marker = original.with_name(original.name + ".attempt.json")
    saved = marker.read_bytes()
    assert json.loads(saved)["command"] == command
    assert b"original inventory" not in saved
    assert cli.main(arguments(c, "propose", original)) == 1
    original.unlink()
    assert cli.main(arguments(c, "proposal-reconcile", original)) == 0
    assert cli.main(arguments(c, "proposal", request_id=command["request_id"])) == 0
    assert marker.read_bytes() == saved
    assert requests == [
        ("POST", path + "/preview"), ("POST", path), ("POST", path + "/reconcile"),
        ("GET", path + "/" + command["request_id"]),
    ]
    output = capsys.readouterr()
    assert "PRIVATE_PROPOSAL_TRANSPORT" not in output.out + output.err
    assert credential.get_token.return_value.token not in output.out + output.err
    assert '"effective": false' in output.out and '"bridge_available": false' in output.out


@pytest.mark.parametrize("interactive,answer", [(False, "share-note"), (True, "cancel")])
def test_cli_requires_explicit_sharing_and_creates_no_marker_on_cancel(
        proposal_case, monkeypatch, tmp_path, interactive, answer):
    c = proposal_case
    path, command = install_case(c)
    credential, requests = environment(c, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    original = tmp_path / "proposal.json"
    original.write_text(json.dumps(command), encoding="utf-8")
    assert cli.main(arguments(c, "propose", original)) == 1
    assert not original.with_name(original.name + ".attempt.json").exists()
    assert requests == ([("POST", path + "/preview")] if interactive else [])
    if not interactive:
        credential.authenticate.assert_not_called()
