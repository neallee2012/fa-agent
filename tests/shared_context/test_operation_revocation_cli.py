"""A human revokes the original grant once; historical lookup never undoes effects."""

import json
from contextlib import contextmanager
from datetime import timedelta
import logging
from types import SimpleNamespace

from azure.core.exceptions import ClientAuthenticationError
import httpx
import pytest

from shared_context.behavior_operations import control_key
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection import approved_revocation, rejection_case
from test_operation_rejection_cli import arguments, operator_environment
from test_skill_guidance_authority import approval_command, operation_path, protected_rows


def revocation_environment(case, monkeypatch, fault=None):
    cli, credential, requests = operator_environment(case, monkeypatch)
    original_client = cli.httpx.Client
    monkeypatch.setattr("builtins.input", lambda _prompt: "revoke")

    def client(**options):
        http = original_client(**options)
        stream = http.stream

        @contextmanager
        def observed(method, path, **kwargs):
            deciding = path.endswith("/revoke")
            if deciding and fault == "before-send":
                raise httpx.ConnectError("PRIVATE_REVOCATION_TRANSPORT")
            with stream(method, path, **kwargs) as response:
                if deciding and fault in {"response-lost", "grant", "revision", "boolean-revision", "revoked", "request"}:
                    response.read()
                    assert response.status_code == 200
                    if fault == "response-lost":
                        raise httpx.ReadTimeout("PRIVATE_REVOCATION_TRANSPORT")
                    value = response.json()
                    if fault == "request":
                        value["control_request_id"] = "0" * 64
                    else:
                        field, replacement = {
                            "grant": ("grant_id", "another-grant"),
                            "revision": ("revocation_revision", 2),
                            "boolean-revision": ("revocation_revision", True),
                            "revoked": ("revoked", False),
                        }[fault]
                        value["result"][field] = replacement
                    yield httpx.Response(200, json=value)
                elif path.endswith("/decision-state") and fault and fault.startswith("state-"):
                    response.read()
                    assert response.status_code == 200
                    value = response.json()
                    if fault == "state-missing":
                        value["grant"] = None
                    elif fault == "state-revoked":
                        value["grant"]["revoked"] = True
                    elif fault == "state-revision":
                        value["grant"]["revocation_revision"] += 1
                    elif fault == "state-operation":
                        value["operation_id"] = "other-operation"
                    elif fault == "state-contract":
                        value["adapter_contract_digest"] = "0" * 64
                    yield httpx.Response(200, json=value)
                else:
                    yield response

        http.stream = observed
        return http

    monkeypatch.setattr(cli.httpx, "Client", client)
    return cli, credential, requests


def test_operator_revokes_the_observed_original_grant_once_and_only_reads_its_receipt(
        rejection_case, monkeypatch, tmp_path, capsys):
    c = rejection_case
    command = approved_revocation(c)
    cli, credential, requests = revocation_environment(c, monkeypatch)
    original = tmp_path / "original-revocation.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    original_rows = protected_rows(c.turn)
    assert cli.main(arguments(c, "revoke", original)) == 0
    attempt = original.with_name(original.name + ".attempt.json")
    retained = attempt.read_bytes()
    saved = json.loads(retained)
    assert saved["schema_version"] == "fa-operation-revocation-cli-attempt-v1"
    assert saved["command"] == command.model_dump(mode="json")
    assert saved["grant_id"] == c.turn.binding.grant_id
    before, guard = protected_rows(c.turn), c.h.store._guard()
    grant = before[control_key("grant", c.turn.binding.grant_id)].payload["data"]
    assert grant["revoked"] is True and grant["revocation_revision"] == 1
    grant_key = control_key("grant", c.turn.binding.grant_id)
    assert all(before[key] == value for key, value in original_rows.items() if key != grant_key)
    assert grant["remaining_uses"] == original_rows[grant_key].payload["data"]["remaining_uses"]
    assert grant["remaining_units"] == original_rows[grant_key].payload["data"]["remaining_units"]
    assert cli.main(arguments(c, "revoke", original)) == 1
    original.unlink()
    assert cli.main(arguments(c, "reconcile", original)) == 0
    assert attempt.read_bytes() == retained
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert requests == [
        ("GET", operation_path(c.turn)),
        ("GET", operation_path(c.turn) + "/decision-state"),
        ("POST", operation_path(c.turn) + "/revoke"),
        ("POST", operation_path(c.turn) + "/revocation-reconcile"),
    ]
    assert credential.authenticate.call_args_list[0].kwargs["scopes"] == [
        "api://22222222-2222-2222-2222-222222222222/Behavior.Read",
        "api://22222222-2222-2222-2222-222222222222/Behavior.Approve",
    ]
    assert credential.authenticate.call_args_list[1].kwargs["scopes"] == [
        "api://22222222-2222-2222-2222-222222222222/Behavior.Read",
    ]
    output = capsys.readouterr()
    assert "original-revocation-receipt-not-effect-outcome" in output.out
    assert credential.get_token.return_value.token not in output.out + output.err
    assert c.effects == [] and c.h.endpoint.requests == []


@pytest.mark.parametrize("fault,confirmed,posts", [
    ("before-send", False, 0), ("response-lost", True, 1), ("grant", True, 1),
    ("revision", True, 1), ("boolean-revision", True, 1), ("revoked", True, 1), ("request", True, 1),
])
def test_revocation_uncertainty_retains_original_revision_and_only_reads_history(
        rejection_case, monkeypatch, tmp_path, capsys, fault, confirmed, posts):
    c = rejection_case
    command = approved_revocation(c)
    cli, credential, requests = revocation_environment(c, monkeypatch, fault)
    original = tmp_path / "revocation.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "revoke", original)) == 1
    attempt = original.with_name(original.name + ".attempt.json")
    retained = attempt.read_bytes()
    assert json.loads(retained)["command"]["expected_revocation_revision"] == 0
    assert cli.main(arguments(c, "revoke", original)) == 1
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert cli.main(arguments(c, "reconcile", original)) == (0 if confirmed else 1)
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert attempt.read_bytes() == retained
    assert requests.count(("POST", operation_path(c.turn) + "/revoke")) == posts
    assert requests.count(("POST", operation_path(c.turn) + "/revocation-reconcile")) == 1
    output = capsys.readouterr()
    assert ('"state": "confirmed"' in output.out) is confirmed
    assert "PRIVATE_REVOCATION_TRANSPORT" not in output.out + output.err
    assert credential.get_token.return_value.token not in output.out + output.err
    assert c.effects == [] and c.h.endpoint.requests == []


@pytest.mark.parametrize("fault", [
    "state-missing", "state-revoked", "state-revision", "state-operation", "state-contract",
])
def test_revocation_does_not_confirm_a_missing_or_mismatched_current_grant(
        rejection_case, monkeypatch, tmp_path, fault):
    c = rejection_case
    command = approved_revocation(c)
    cli, _, requests = revocation_environment(c, monkeypatch, fault)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("No matching grant to confirm"))
    original = tmp_path / "revocation.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert cli.main(arguments(c, "revoke", original)) == 1
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert requests == [("GET", operation_path(c.turn)), ("GET", operation_path(c.turn) + "/decision-state")]
    assert not original.with_name(original.name + ".attempt.json").exists()


@pytest.mark.parametrize("interactive,answer", [(False, "revoke"), (True, "cancel")])
def test_revocation_requires_its_own_interactive_confirmation(
        rejection_case, monkeypatch, tmp_path, interactive, answer):
    c = rejection_case
    command = approved_revocation(c)
    cli, credential, requests = revocation_environment(c, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    original = tmp_path / "revocation.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    before = protected_rows(c.turn)
    assert cli.main(arguments(c, "revoke", original)) == 1
    assert protected_rows(c.turn) == before and not original.with_name(original.name + ".attempt.json").exists()
    assert requests == ([("GET", operation_path(c.turn)), ("GET", operation_path(c.turn) + "/decision-state")] if interactive else [])
    if not interactive:
        credential.authenticate.assert_not_called()


@pytest.mark.parametrize("change", [
    {"expected_revocation_revision": True}, {"expected_revocation_revision": -1},
    {"reason": "caller supplied authority"}, {"revoke_all": True},
])
def test_revocation_rejects_malformed_original_commands_before_authentication(
        rejection_case, monkeypatch, tmp_path, change):
    c = rejection_case
    command = approved_revocation(c)
    cli, credential, requests = revocation_environment(c, monkeypatch)
    original = tmp_path / "revocation.json"
    original.write_text(json.dumps({**command.model_dump(mode="json"), **change}), encoding="utf-8")
    assert cli.main(arguments(c, "revoke", original)) == 1
    assert not requests and not original.with_name(original.name + ".attempt.json").exists()
    credential.authenticate.assert_not_called()


def test_revocation_reconcile_does_not_replace_the_saved_revision(rejection_case, monkeypatch, tmp_path):
    c = rejection_case
    command = approved_revocation(c)
    cli, credential, requests = revocation_environment(c, monkeypatch)
    original = tmp_path / "revocation.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "revoke", original)) == 0
    before = list(requests)
    retained = original.with_name(original.name + ".attempt.json").read_bytes()
    original.write_text(command.model_copy(update={"expected_revocation_revision": 1}).model_dump_json(), encoding="utf-8")
    credential.authenticate.reset_mock()
    assert cli.main(arguments(c, "reconcile", original)) == 1
    assert requests == before and original.with_name(original.name + ".attempt.json").read_bytes() == retained
    credential.authenticate.assert_not_called()


def test_revocation_token_failure_after_observation_keeps_the_original_attempt(
        rejection_case, monkeypatch, tmp_path, capsys, caplog):
    c = rejection_case
    command = approved_revocation(c)
    cli, credential, requests = revocation_environment(c, monkeypatch)
    token = credential.get_token.return_value.token
    tokens = []

    def get_token(*scopes):
        tokens.append(scopes)
        if len(tokens) == 3:
            logging.getLogger("azure.identity").warning("PRIVATE_REVOCATION_CREDENTIAL")
            raise ClientAuthenticationError("PRIVATE_REVOCATION_CREDENTIAL")
        return SimpleNamespace(token=token)

    credential.get_token.side_effect = get_token
    original = tmp_path / "revocation.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    before = protected_rows(c.turn)
    assert cli.main(arguments(c, "revoke", original)) == 1
    retained = original.with_name(original.name + ".attempt.json").read_bytes()
    assert protected_rows(c.turn) == before
    assert cli.main(arguments(c, "reconcile", original)) == 1
    assert original.with_name(original.name + ".attempt.json").read_bytes() == retained
    assert requests == [
        ("GET", operation_path(c.turn)), ("GET", operation_path(c.turn) + "/decision-state"),
        ("POST", operation_path(c.turn) + "/revocation-reconcile"),
    ]
    output = capsys.readouterr()
    assert "PRIVATE_REVOCATION_CREDENTIAL" not in output.out + output.err + caplog.text
    assert token not in output.out + output.err + caplog.text
    assert c.effects == []


def test_expired_grant_can_be_explicitly_revoked_without_changing_its_expiry_or_units(
        rejection_case, monkeypatch, tmp_path):
    from shared_context.behavior_wire import OperationRevocation

    c = rejection_case
    approval = approval_command(c.turn, c.prepared).model_copy(update={"expires_at": c.h.time + timedelta(seconds=2)})
    assert c.h.client.post(operation_path(c.turn) + "/approve",
        headers=c.h.headers(), content=approval.model_dump_json()).status_code == 200
    c.h.time += timedelta(seconds=3)
    command = OperationRevocation(request_id="expired-revoke", decision_id="expired-revoke",
        description_digest=approval.description_digest, expected_revocation_revision=0)
    cli, _, requests = revocation_environment(c, monkeypatch)
    original = tmp_path / "revocation.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    key = control_key("grant", c.turn.binding.grant_id)
    before = protected_rows(c.turn)[key].payload["data"]
    assert cli.main(arguments(c, "revoke", original)) == 0
    after = protected_rows(c.turn)[key].payload["data"]
    assert after == {**before, "revoked": True, "revocation_revision": 1}
    assert requests[-1] == ("POST", operation_path(c.turn) + "/revoke")
    assert c.effects == [] and c.h.endpoint.requests == []
