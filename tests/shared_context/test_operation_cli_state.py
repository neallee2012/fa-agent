"""The operator state command displays validated facts without creating authority."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_wire import OperationDecisionState
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection import approved_revocation, rejection_case
from test_operation_rejection_cli import arguments, operator_environment
from test_skill_guidance_authority import operation_path, protected_rows


def test_state_command_uses_only_current_read_scope_and_never_changes_a_grant(
        rejection_case, monkeypatch, capsys):
    c = rejection_case
    revocation = approved_revocation(c)
    assert c.h.client.post(operation_path(c.turn) + "/revoke",
        headers=c.h.headers(), content=revocation.model_dump_json()).status_code == 200
    cli, credential, requests = operator_environment(c, monkeypatch)
    credential.get_token.return_value = SimpleNamespace(
        token=c.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    before, guard, batches = protected_rows(c.turn), c.h.store._guard(), len(c.h.transport.parts)
    assert cli.main(arguments(c, "state", None)[:-2]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == "fa-operation-decision-state-v1"
    assert output["purpose"] == "observation-only-not-operation-permission"
    assert output["binding_id"] == c.turn.binding.binding_id and output["grant"]["revoked"] is True
    assert output["grant"]["revocation_revision"] == 1
    assert "can_execute" not in output and "can_reject" not in output
    assert requests == [("GET", operation_path(c.turn) + "/decision-state")]
    credential.authenticate.assert_called_once_with(
        scopes=["api://22222222-2222-2222-2222-222222222222/Behavior.Read"])
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert len(c.h.transport.parts) == batches and c.effects == []


@pytest.mark.parametrize("change", [
    {"binding_id": "another-binding"},
    {"schema_version": "unknown-future-format"},
    {"purpose": "permission-granted"},
    {"can_execute": True},
    {"grant": {"subjects": [{"principal_id": "PRIVATE_RESPONSE_CANARY"}]}},
    {"schema_version": None},
    {"purpose": None},
])
def test_unverifiable_state_is_never_partially_printed_or_used_for_an_action(
        rejection_case, monkeypatch, capsys, change):
    c = rejection_case
    approved_revocation(c)
    cli, _, requests = operator_environment(c, monkeypatch)
    original_client = cli.httpx.Client

    def client(**options):
        http = original_client(**options)
        original_stream = http.stream

        @contextmanager
        def altered(method, path, **kwargs):
            with original_stream(method, path, **kwargs) as response:
                response.read()
                assert response.status_code == 200
                data = response.json()
                OperationDecisionState.model_validate_json(response.content)
                for name, value in change.items():
                    if value is None:
                        data.pop(name)
                    elif name == "grant":
                        data["grant"] = {**data["grant"], **value}
                    else:
                        data[name] = value
                yield httpx.Response(200, json=data)

        http.stream = altered
        return http

    monkeypatch.setattr(cli.httpx, "Client", client)
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert cli.main(arguments(c, "state", None)[:-2]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "OPERATION_CLI_UNCONFIRMED" in output.err
    assert "PRIVATE_RESPONSE_CANARY" not in output.err
    assert requests == [("GET", operation_path(c.turn) + "/decision-state")]
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard and c.effects == []


@pytest.mark.parametrize("failure", [403, 404, 503, "transport"])
def test_unavailable_state_never_falls_back_to_preview_or_retries(rejection_case, monkeypatch, capsys, failure):
    c = rejection_case
    cli, _, requests = operator_environment(c, monkeypatch)
    original_client = cli.httpx.Client

    def client(**options):
        http = original_client(**options)
        original_stream = http.stream

        @contextmanager
        def failed(method, path, **kwargs):
            with original_stream(method, path, **kwargs) as response:
                response.read()
                if failure == "transport":
                    raise httpx.ReadTimeout("PRIVATE_STATE_TRANSPORT")
                yield httpx.Response(failure, json={"error": {"message": "PRIVATE_STATE_TRANSPORT"}})

        http.stream = failed
        return http

    monkeypatch.setattr(cli.httpx, "Client", client)
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert cli.main(arguments(c, "state", None)[:-2]) == 1
    output = capsys.readouterr()
    assert output.out == "" and "PRIVATE_STATE_TRANSPORT" not in output.err
    assert requests == [("GET", operation_path(c.turn) + "/decision-state")]
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard


def test_unapproved_state_remains_observation_only_without_an_attempt_file(
        rejection_case, monkeypatch, capsys, tmp_path):
    c = rejection_case
    cli, _, requests = operator_environment(c, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    assert cli.main(arguments(c, "state", None)[:-2]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["grant"] is None and value["effect_state"] == "unknown"
    assert value["purpose"] == "observation-only-not-operation-permission"
    assert requests == [("GET", operation_path(c.turn) + "/decision-state")]
    assert set(tmp_path.iterdir()) == before and c.effects == []
