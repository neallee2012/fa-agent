from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json

import httpx
import pytest

from conftest import A
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_cli import main
from shared_context.m3_client import ArtifactClient
from shared_context.m3_models import SkillDeactivationInput
from test_m3_artifacts import m3
from test_m3_auth import jwk_client, signing_key
from test_skill_deactivation import D1, V1, deactivation_api, deactivation_body


def consumer_parts(case):
    client, _, human, _, _, _ = case
    consumer = ArtifactClient(client, A.team, lambda: human["Authorization"].removeprefix("Bearer "))
    snapshot = consumer.get("deactivation-skill")
    body = SkillDeactivationInput.model_validate(deactivation_body(snapshot.payload["versions"][0]))
    return consumer, snapshot, body


def test_deactivation_client_requires_the_original_decision_receipt(deactivation_api):
    consumer, snapshot, body = consumer_parts(deactivation_api)
    with patch.object(consumer.http, "stream", wraps=consumer.http.stream) as calls:
        saved = consumer.deactivate("deactivation-skill", snapshot, body)
    assert calls.call_count == 1 and saved.payload["effective_version_id"] is None
    assert saved.payload["versions"] == snapshot.payload["versions"]
    assert saved.payload["deactivations"][0]["decision_id"] == D1
    assert consumer.deactivate("deactivation-skill", snapshot, body) == saved


def test_deactivation_client_never_replays_an_unknown_post(deactivation_api):
    consumer, snapshot, body = consumer_parts(deactivation_api)
    original = consumer.http.stream
    calls = []

    def lost_response(method, path, **kwargs):
        calls.append((method, path))
        with original(method, path, **kwargs) as response:
            response.read()
            assert response.status_code == 200
        raise httpx.ReadTimeout("synthetic acknowledgement loss")

    with patch.object(consumer.http, "stream", side_effect=lost_response):
        with pytest.raises(Problem) as unknown:
            consumer.deactivate("deactivation-skill", snapshot, body)
    assert unknown.value.body["error"]["code"] == "M3_REQUEST_UNKNOWN"
    assert len(calls) == 1 and calls[0][0] == "POST"
    readback = consumer.get("deactivation-skill")
    assert readback.payload["effective_version_id"] is None
    assert readback.payload["versions"] == snapshot.payload["versions"]
    assert len(readback.payload["deactivations"]) == 1


def test_deactivation_client_does_not_accept_a_missing_effect_receipt(deactivation_api):
    consumer, snapshot, body = consumer_parts(deactivation_api)
    original = consumer._request

    def missing_receipt(*args, **kwargs):
        headers, data = original(*args, **kwargs)
        parsed = json.loads(data)
        parsed.pop("deactivations")
        return headers, json.dumps(parsed).encode("utf-8")

    with patch.object(consumer, "_request", side_effect=missing_receipt):
        with pytest.raises(Problem) as error:
            consumer.deactivate("deactivation-skill", snapshot, body)
    assert error.value.body["error"]["code"] == "M3_DEACTIVATION_UNKNOWN"
    assert consumer.get("deactivation-skill").payload["deactivations"][0]["decision_id"] == D1


def test_cli_refuses_noninteractive_deactivation_before_authentication(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with patch("shared_context.m3_cli.InteractiveBrowserCredential") as credential:
        result = main([
            "--api", "https://poc.example.westus3.azurecontainerapps.io",
            "--tenant", "11111111-1111-1111-1111-111111111111",
            "--audience", "api://22222222-2222-2222-2222-222222222222",
            "--client-id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "--team", A.team,
            "deactivate", "deactivation-skill", "--version", V1, "--sha256", "0" * 64,
        ])
    assert result == 1
    credential.assert_not_called()


@pytest.mark.parametrize("answer,expected", [("deactivate", 0), ("cancel", 1)])
def test_human_cli_confirms_only_the_exact_current_skill_version(
        deactivation_api, monkeypatch, capsys, answer, expected):
    http, _, human, service, _, snapshot = deactivation_api
    version = snapshot.json()["versions"][0]
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(token=human["Authorization"].removeprefix("Bearer "))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def confirm(prompt):
        preview = capsys.readouterr().out
        assert version["sha256"] in preview and version["metadata_sha256"] in preview
        assert V1 in preview and "保留" in preview and "停用" in prompt
        return answer

    with patch("shared_context.m3_cli.InteractiveBrowserCredential", return_value=credential), \
            patch("shared_context.m3_cli.httpx.Client", return_value=http), \
            patch("builtins.input", side_effect=confirm), \
            patch.object(service.store, "read", side_effect=AssertionError("deactivation needs no blob download")):
        result = main([
            "--api", "https://poc.example.westus3.azurecontainerapps.io",
            "--tenant", "11111111-1111-1111-1111-111111111111",
            "--audience", "api://22222222-2222-2222-2222-222222222222",
            "--client-id", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "--team", A.team,
            "deactivate", "deactivation-skill", "--version", V1,
            "--sha256", version["sha256"], "--metadata-sha256", version["metadata_sha256"],
            "--decision-id", D1,
        ])
    assert result == expected
    current = service.get(HumanApprover(service.approval.approver_oid, A.team), A.team, "deactivation-skill")
    assert current.payload["effective_version_id"] == (None if expected == 0 else V1)
    assert current.payload["versions"] == snapshot.json()["versions"]
