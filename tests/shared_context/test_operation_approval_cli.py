"""Human approval CLI uses the original API and receipt, never executes effects."""

import json
from contextlib import contextmanager
from datetime import timedelta
import logging
from types import SimpleNamespace

from azure.core.exceptions import ClientAuthenticationError
import httpx
import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_operations import ContentDependency, OperationDescription, ResolvedResource
from shared_context.behavior_runtime import material
from shared_context.behavior_store import InlineMaterial
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_operation_rejection import rejection_case
from test_operation_rejection_cli import arguments, operator_environment
from test_skill_guidance_authority import approval_command, operation_path, protected_rows


def approval_environment(case, monkeypatch, fault=None):
    cli, credential, requests = operator_environment(case, monkeypatch)
    original_client = cli.httpx.Client
    monkeypatch.setattr("builtins.input", lambda _prompt: "approve")

    def client(**options):
        http = original_client(**options)
        stream = http.stream

        @contextmanager
        def observed(method, path, **kwargs):
            deciding = path.endswith("/approve")
            if deciding and fault == "before-send":
                raise httpx.ConnectError("PRIVATE_APPROVAL_TRANSPORT")
            with stream(method, path, **kwargs) as response:
                if deciding and fault in {
                    "response-lost", "bad-receipt", "wrong-approval-id", "wrong-digest",
                    "boolean-uses", "wrong-control-id", "invalid-grant",
                }:
                    response.read()
                    assert response.status_code == 200
                    if fault == "response-lost":
                        raise httpx.ReadTimeout("PRIVATE_APPROVAL_TRANSPORT")
                    if fault == "bad-receipt":
                        payload = {"result": None, "private": "PRIVATE_APPROVAL_TRANSPORT"}
                    else:
                        payload = response.json()
                        if fault == "wrong-control-id":
                            payload["control_request_id"] = "0" * 64
                        else:
                            field, value = {
                                "wrong-approval-id": ("approval_id", "other-approval"),
                                "wrong-digest": ("description_digest", "0" * 64),
                                "boolean-uses": ("approved_uses", True),
                                "invalid-grant": ("grant_id", "*"),
                            }[fault]
                            payload["result"][field] = value
                    yield httpx.Response(200, json=payload)
                else:
                    yield response

        http.stream = observed
        return http

    monkeypatch.setattr(cli.httpx, "Client", client)
    return cli, credential, requests


def test_operator_approves_the_complete_original_description_once_then_reads_history(
        rejection_case, monkeypatch, tmp_path, capsys):
    c = rejection_case
    cli, credential, requests = approval_environment(c, monkeypatch)
    command = approval_command(c.turn, c.prepared)
    original = tmp_path / "original-approval.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    before = original.read_bytes()
    assert cli.main(arguments(c, "approve", original)) == 0
    output = capsys.readouterr()
    preview, _ = json.JSONDecoder().raw_decode(output.out)
    material = InlineMaterial.model_validate_json(json.dumps(c.preview["content"]))
    description = OperationDescription.model_validate_json(material.data)
    assert preview["description"] == description.model_dump(mode="json")
    assert preview["description_digest"] == material.sha256 == command.description_digest
    assert preview["adapter_contract_digest"] == command.adapter_contract_digest
    assert preview["material_byte_length"] == len(material.data)
    assert "resources" in preview["description"] and "dependencies" in preview["description"]

    attempt = original.with_name(original.name + ".attempt.json")
    retained = attempt.read_bytes()
    assert json.loads(retained)["command"] == command.model_dump(mode="json")
    assert cli.main(arguments(c, "approve", original)) == 1
    assert cli.main(arguments(c, "reconcile", original)) == 0
    assert original.read_bytes() == before and attempt.read_bytes() == retained
    assert requests == [
        ("GET", operation_path(c.turn)),
        ("POST", operation_path(c.turn) + "/approve"),
        ("POST", operation_path(c.turn) + "/approval-reconcile"),
    ]
    scopes = credential.authenticate.call_args_list
    assert scopes[0].kwargs["scopes"] == [
        "api://22222222-2222-2222-2222-222222222222/Behavior.Read",
        "api://22222222-2222-2222-2222-222222222222/Behavior.Approve",
    ]
    assert scopes[1].kwargs["scopes"] == ["api://22222222-2222-2222-2222-222222222222/Behavior.Read"]
    after = capsys.readouterr()
    assert "original-approval-receipt-not-current-permission" in output.out + after.out
    assert credential.get_token.return_value.token not in output.out + output.err + after.out + after.err
    assert c.effects == [] and c.h.endpoint.requests == []


@pytest.mark.parametrize("fault,expected,posts", [
    ("before-send", 1, 0), ("response-lost", 0, 1), ("bad-receipt", 0, 1),
])
def test_approval_unknown_keeps_the_original_command_and_never_reposts(
        rejection_case, monkeypatch, tmp_path, capsys, fault, expected, posts):
    c = rejection_case
    cli, credential, requests = approval_environment(c, monkeypatch, fault)
    command = approval_command(c.turn, c.prepared)
    original = tmp_path / "approval.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 1
    attempt = original.with_name(original.name + ".attempt.json")
    retained = attempt.read_bytes()
    assert json.loads(retained)["command"] == command.model_dump(mode="json")
    assert cli.main(arguments(c, "approve", original)) == 1
    original.unlink()
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert cli.main(arguments(c, "reconcile", original)) == expected
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert attempt.read_bytes() == retained
    assert requests.count(("POST", operation_path(c.turn) + "/approve")) == posts
    assert requests.count(("POST", operation_path(c.turn) + "/approval-reconcile")) == 1
    output = capsys.readouterr()
    assert ('"state": "confirmed"' in output.out) == (expected == 0)
    assert "PRIVATE_APPROVAL_TRANSPORT" not in output.out + output.err
    assert credential.get_token.return_value.token not in output.out + output.err
    assert c.effects == [] and not c.h.endpoint.requests


@pytest.mark.parametrize("interactive,answer", [(False, "approve"), (True, "cancel")])
def test_approval_requires_explicit_interactive_confirmation(
        rejection_case, monkeypatch, tmp_path, interactive, answer):
    c = rejection_case
    cli, credential, requests = approval_environment(c, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    original = tmp_path / "approval.json"
    original.write_text(approval_command(c.turn, c.prepared).model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 1
    assert requests == ([("GET", operation_path(c.turn))] if interactive else [])
    assert not original.with_name(original.name + ".attempt.json").exists()
    if not interactive:
        credential.authenticate.assert_not_called()
    assert not c.effects


@pytest.mark.parametrize("change", [
    {"expires_at": "2026-09-27T12:00:00"},
    {"expires_at": "2026-09-27T12:00:00+01:00"},
    {"uses": 2},
    {"units": 2},
    {"uses": True},
    {"units": True},
    {"expires_at": 0},
    {"approved_by_oid": "caller-supplied"},
])
def test_malformed_approval_never_authenticates_or_creates_an_attempt(
        rejection_case, monkeypatch, tmp_path, change):
    c = rejection_case
    cli, credential, requests = approval_environment(c, monkeypatch)
    body = approval_command(c.turn, c.prepared).model_dump(mode="json")
    original = tmp_path / "approval.json"
    original.write_text(json.dumps({**body, **change}), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 1
    assert not requests and not original.with_name(original.name + ".attempt.json").exists()
    credential.authenticate.assert_not_called()


@pytest.mark.parametrize("field", ["description_digest", "adapter_contract_digest"])
def test_approval_does_not_confirm_stale_material(rejection_case, monkeypatch, tmp_path, field):
    c = rejection_case
    cli, _, requests = approval_environment(c, monkeypatch)
    body = approval_command(c.turn, c.prepared).model_dump(mode="json")
    original = tmp_path / "approval.json"
    original.write_text(json.dumps({**body, field: "0" * 64}), encoding="utf-8")
    before = protected_rows(c.turn)
    assert cli.main(arguments(c, "approve", original)) == 1
    assert requests == [("GET", operation_path(c.turn))]
    assert not original.with_name(original.name + ".attempt.json").exists()
    assert protected_rows(c.turn) == before


@pytest.mark.parametrize("change", ["request", "expiry", "binding", "api"])
def test_original_approval_attempt_cannot_be_rebound(
        rejection_case, monkeypatch, tmp_path, change):
    c = rejection_case
    cli, credential, requests = approval_environment(c, monkeypatch)
    command = approval_command(c.turn, c.prepared)
    original = tmp_path / "approval.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 0
    before = list(requests)
    credential.authenticate.reset_mock()
    args = arguments(c, "reconcile", original)
    if change in {"request", "expiry"}:
        update = ({"request_id": "different-request"} if change == "request" else
                  {"expires_at": command.expires_at + timedelta(seconds=1)})
        original.write_text(command.model_copy(update=update).model_dump_json(), encoding="utf-8")
    elif change == "binding":
        args[args.index("reconcile") + 1] = "another-binding"
    else:
        args[args.index("--api") + 1] = "https://other.example.westus3.azurecontainerapps.io"
    assert cli.main(args) == 1
    assert requests == before
    credential.authenticate.assert_not_called()


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_original_approval_receipt_does_not_restore_current_permission(
        rejection_case, monkeypatch, tmp_path, capsys, state):
    c = rejection_case
    cli, _, requests = approval_environment(c, monkeypatch)
    command = approval_command(c.turn, c.prepared).model_copy(update={"expires_at": c.h.time + timedelta(seconds=2)})
    original = tmp_path / "approval.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 0
    if state == "expired":
        c.h.time += timedelta(seconds=3)
    else:
        revoked = c.h.client.post(operation_path(c.turn) + "/revoke", headers=c.h.headers(),
            content=wire.OperationRevocation(
                request_id="revoke-cli-approval", decision_id="revoke-cli-approval",
                description_digest=command.description_digest, expected_revocation_revision=0,
            ).model_dump_json())
        assert revoked.status_code == 200
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert cli.main(arguments(c, "reconcile", original)) == 0
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert requests.count(("POST", operation_path(c.turn) + "/approve")) == 1
    assert "original-approval-receipt-not-current-permission" in capsys.readouterr().out
    assert c.effects == []


def test_approval_preview_keeps_complete_binary_content_and_dependency_bytes(
        rejection_case, monkeypatch, tmp_path, capsys):
    c = rejection_case
    cli, _, requests = approval_environment(c, monkeypatch)
    description = OperationDescription(
        adapter_id="reviewed-adapter", adapter_version="adapter-v1", kind="script", method="RUN",
        target="logical-target", content=b"\xff\x00original-bytes",
        resources=(ResolvedResource(resource_id="logical-resource", version="resource-v1"),),
        dependencies=(ContentDependency(resource_id="dependency", version="dep-v1", content=b"data\n\x1b[0m"),),
    )
    prepared = material(description.model_dump(mode="json"))
    original_client = cli.httpx.Client

    def client(**options):
        http = original_client(**options)

        @contextmanager
        def preview_only(method, path, **kwargs):
            assert method == "GET" and path == operation_path(c.turn)
            requests.append((method, path))
            yield httpx.Response(200, json={
                **c.preview, "content": prepared.model_dump(mode="json"), "description_digest": prepared.sha256,
            })

        http.stream = preview_only
        return http

    monkeypatch.setattr(cli.httpx, "Client", client)
    monkeypatch.setattr("builtins.input", lambda _prompt: "cancel")
    command = approval_command(c.turn, c.prepared).model_copy(update={"description_digest": prepared.sha256})
    original = tmp_path / "approval.json"
    original.write_text(command.model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 1
    output = capsys.readouterr()
    preview, _ = json.JSONDecoder().raw_decode(output.out)
    assert preview["description"] == description.model_dump(mode="json")
    assert preview["content_view"]["encoding"] == "base64"
    assert preview["content_view"]["byte_length"] == len(description.content)
    assert preview["dependency_views"][0]["text"] == description.dependencies[0].content.decode("utf-8")
    assert "\x1b" not in output.out
    assert not original.with_name(original.name + ".attempt.json").exists()
    assert requests == [("GET", operation_path(c.turn))] and not c.effects


@pytest.mark.parametrize("fault", [
    "wrong-approval-id", "wrong-digest", "boolean-uses", "wrong-control-id", "invalid-grant",
])
def test_mismatched_approval_receipt_does_not_confirm_the_decision(
        rejection_case, monkeypatch, tmp_path, capsys, fault):
    c = rejection_case
    cli, _, requests = approval_environment(c, monkeypatch, fault)
    original = tmp_path / "approval.json"
    original.write_text(approval_command(c.turn, c.prepared).model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 1
    assert '"state": "confirmed"' not in capsys.readouterr().out
    retained = original.with_name(original.name + ".attempt.json").read_bytes()
    before, guard = protected_rows(c.turn), c.h.store._guard()
    assert cli.main(arguments(c, "reconcile", original)) == 0
    assert protected_rows(c.turn) == before and c.h.store._guard() == guard
    assert original.with_name(original.name + ".attempt.json").read_bytes() == retained
    assert requests.count(("POST", operation_path(c.turn) + "/approve")) == 1 and not c.effects


@pytest.mark.parametrize("failure", ["race", "flush"])
def test_approval_attempt_must_be_durable_without_overwriting_an_existing_attempt(
        rejection_case, monkeypatch, tmp_path, failure):
    c = rejection_case
    cli, _, requests = approval_environment(c, monkeypatch)
    original = tmp_path / "approval.json"
    original.write_text(approval_command(c.turn, c.prepared).model_dump_json(), encoding="utf-8")
    attempt = original.with_name(original.name + ".attempt.json")
    if failure == "race":
        def confirm(_prompt):
            attempt.write_bytes(b"another original attempt")
            return "approve"
        monkeypatch.setattr("builtins.input", confirm)
    else:
        def fail_flush(_descriptor):
            raise OSError("PRIVATE_APPROVAL_DISK")
        monkeypatch.setattr(cli.os, "fsync", fail_flush)
    before = protected_rows(c.turn)
    assert cli.main(arguments(c, "approve", original)) == 1
    assert requests == [("GET", operation_path(c.turn))]
    assert protected_rows(c.turn) == before and attempt.exists()
    if failure == "race":
        assert attempt.read_bytes() == b"another original attempt"


def test_late_approval_token_failure_preserves_attempt_without_logging_private_details(
        rejection_case, monkeypatch, tmp_path, capsys, caplog):
    c = rejection_case
    cli, credential, requests = approval_environment(c, monkeypatch)
    token = credential.get_token.return_value.token
    token_calls = []

    def get_token(*scopes):
        token_calls.append(scopes)
        if len(token_calls) == 2:
            logging.getLogger("azure.identity").warning("PRIVATE_APPROVAL_CREDENTIAL_DETAIL")
            raise ClientAuthenticationError("PRIVATE_APPROVAL_CREDENTIAL_DETAIL")
        return SimpleNamespace(token=token)

    credential.get_token.side_effect = get_token
    original = tmp_path / "approval.json"
    original.write_text(approval_command(c.turn, c.prepared).model_dump_json(), encoding="utf-8")
    assert cli.main(arguments(c, "approve", original)) == 1
    retained = original.with_name(original.name + ".attempt.json").read_bytes()
    assert cli.main(arguments(c, "reconcile", original)) == 1
    assert original.with_name(original.name + ".attempt.json").read_bytes() == retained
    assert requests == [
        ("GET", operation_path(c.turn)),
        ("POST", operation_path(c.turn) + "/approval-reconcile"),
    ]
    output = capsys.readouterr()
    assert "PRIVATE_APPROVAL_CREDENTIAL_DETAIL" not in output.out + output.err + caplog.text
    assert token not in output.out + output.err + caplog.text
    assert c.effects == []


def test_requested_cli_scope_does_not_override_a_read_only_token(rejection_case, monkeypatch, tmp_path):
    c = rejection_case
    cli, credential, requests = approval_environment(c, monkeypatch)
    credential.get_token.return_value = SimpleNamespace(
        token=c.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    original = tmp_path / "approval.json"
    original.write_text(approval_command(c.turn, c.prepared).model_dump_json(), encoding="utf-8")
    before = protected_rows(c.turn)
    assert cli.main(arguments(c, "approve", original)) == 1
    assert protected_rows(c.turn) == before
    assert requests == [
        ("GET", operation_path(c.turn)),
        ("POST", operation_path(c.turn) + "/approve"),
    ]
    assert c.effects == []


def test_rejected_operation_is_not_offered_as_a_new_approval(rejection_case, monkeypatch, tmp_path):
    c = rejection_case
    assert c.h.client.post(operation_path(c.turn) + "/reject", json=c.command, headers=c.h.headers()).status_code == 200
    cli, _, requests = approval_environment(c, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("Rejected operations cannot be approved"))
    original = tmp_path / "approval.json"
    original.write_text(approval_command(c.turn, c.prepared).model_dump_json(), encoding="utf-8")
    before = protected_rows(c.turn)
    assert cli.main(arguments(c, "approve", original)) == 1
    assert protected_rows(c.turn) == before and not c.effects
    assert requests == [("GET", operation_path(c.turn))]
    assert not original.with_name(original.name + ".attempt.json").exists()
