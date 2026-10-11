"""Explicit new-conversation memory settings preserve installed policy and old work."""

import importlib
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import TENANT
from shared_context import behavior_wire as wire
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import CLIENT_ID, jwk_client, signing_key
from test_memory_note_proposals import proposal_case


SETTINGS = "/operator/v1/memory-bridge-settings"


def install_settings(case):
    api = importlib.import_module("shared_context.memory_bridge_settings_api")
    api.install_memory_bridge_settings_routes(case.h.app, case.h.runtime, case.h.verifier)


def test_human_selects_versioned_settings_without_changing_old_work_or_policy(proposal_case):
    c = proposal_case
    install_settings(c)
    original_policy = c.control.installed()
    original_work = c.control.records[c.binding.binding_id]
    original_runtime_policy = c.h.runtime.authorize(c.binding.binding_id, c.human, "read")[1]
    command = {
        "schema_version": "fa-memory-bridge-settings-command-v1",
        "request_id": "enable-new-memory-conversations",
        "expected_settings_id": None,
        "enabled": True,
    }
    response = c.h.client.post(SETTINGS, json=command, headers=c.h.headers())
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["enabled"] is True
    assert result["applies_to"] == "new-conversations-only"
    assert result["data_scope_change"] is True
    assert result["recall"] is None
    guard = c.h.store._guard()
    assert c.h.client.post(SETTINGS, json=command, headers=c.h.headers()).json() == response.json()
    lookup = c.h.client.post(SETTINGS + "/reconcile", json=command, headers=c.h.headers("read-only"))
    assert lookup.json() == response.json()
    assert c.h.store._guard() == guard
    current = c.h.client.get(SETTINGS, headers=c.h.headers("read-only"))
    assert current.status_code == 200 and current.json()["settings_id"] == result["settings_id"]
    assert c.control.installed() == original_policy
    assert c.control.records[c.binding.binding_id] == original_work
    assert c.h.runtime.authorize(c.binding.binding_id, c.human, "read")[1] == original_runtime_policy
    assert original_runtime_policy.human_text_enabled is False
    assert c.h.endpoint.requests == []


def settings_command(case, *, recall=False):
    command = {
        "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "select-recall",
        "expected_settings_id": None, "enabled": True,
    }
    if recall:
        path = "/behavior/v1/bindings/" + case.binding.binding_id
        note = case.h.client.get(path + "/claims/" + case.binding.claim_ids[0], headers=case.h.headers())
        assert note.status_code == 200
        reference = note.json()["reference"]
        command["recall"] = {
            "origin_binding_id": case.binding.binding_id, "claim_id": reference["claim_id"],
            "expected_claim_version": reference["version"], "expected_claim_sha256": reference["sha256"],
            "use_in_new_conversations": True,
        }
    return command


def test_explicit_note_selection_records_one_exact_choice_and_keeps_original_bytes(proposal_case):
    c = proposal_case
    install_settings(c)
    command = settings_command(c, recall=True)
    guard = c.h.store._guard()
    preview = c.h.client.post(SETTINGS + "/preview", json=command, headers=c.h.headers("read-only"))
    assert preview.status_code == 200, preview.text
    assert preview.json()["note"]["source"]["claim_sha256"] == command["recall"]["expected_claim_sha256"]
    assert c.h.store._guard() == guard
    selected = c.h.client.post(SETTINGS, json=command, headers=c.h.headers())
    assert selected.status_code == 200, selected.text
    assert selected.json()["result"]["recall"] == {
        "origin_binding_id": c.binding.binding_id, "claim_id": c.binding.claim_ids[0],
        "version_policy": "current-human-reviewed-version-at-new-conversation",
    }
    changed = {**command, "enabled": False, "recall": None}
    guard = c.h.store._guard()
    assert c.h.client.post(SETTINGS, json=changed, headers=c.h.headers()).status_code == 409
    assert c.h.client.post(SETTINGS + "/reconcile", json=changed, headers=c.h.headers("read-only")).status_code == 409
    assert c.h.store._guard() == guard


def test_old_approved_selection_is_retained_when_new_conversation_defaults_are_disabled(proposal_case):
    from shared_context.memory_bridge_settings import read_memory_settings

    c = proposal_case
    install_settings(c)
    command = settings_command(c)
    first = c.h.client.post(SETTINGS, json=command, headers=c.h.headers())
    assert first.status_code == 200
    first_id = first.json()["result"]["settings_id"]
    captured = read_memory_settings(c.h.store, c.control.policy, first_id)
    disabled = {**command, "request_id": "disable-future", "expected_settings_id": first_id, "enabled": False}
    second = c.h.client.post(SETTINGS, json=disabled, headers=c.h.headers())
    assert second.status_code == 200 and second.json()["result"]["revision"] == 2
    assert c.h.client.get(SETTINGS, headers=c.h.headers("read-only")).json()["enabled"] is False
    assert read_memory_settings(c.h.store, c.control.policy, first_id) == captured
    assert c.h.client.post(SETTINGS + "/reconcile", json=command,
                           headers=c.h.headers("read-only")).json() == first.json()


@pytest.mark.parametrize("change,status", [
    ({"enabled": 1}, 422),
    ({"expected_settings_id": "not-the-current-setting"}, 409),
    ({"enabled": False}, 422),
    ({"recall": {"use_in_new_conversations": False}}, 422),
    ({"recall": {"expected_claim_sha256": "0" * 64}}, 409),
    ({"recall": {"origin_binding_id": "another-work"}}, 403),
])
def test_setting_denies_bad_consent_stale_note_and_foreign_scope_without_writes(proposal_case, change, status):
    c = proposal_case
    install_settings(c)
    command = settings_command(c, recall=True)
    command = {**command, **change, "recall": {**command["recall"], **change.get("recall", {})}}
    guard = c.h.store._guard()
    result = c.h.client.post(SETTINGS, json=command, headers=c.h.headers())
    assert result.status_code == status
    assert c.h.store._guard() == guard


@pytest.mark.parametrize("identity", ["agent", "read-only"])
def test_only_configured_human_approve_can_change_new_conversation_memory_scope(proposal_case, identity):
    c = proposal_case
    install_settings(c)
    guard = c.h.store._guard()
    result = c.h.client.post(SETTINGS, json=settings_command(c), headers=c.h.headers(identity))
    assert result.status_code == 403 and c.h.store._guard() == guard


def test_ack_loss_only_reads_the_original_setting_receipt(proposal_case, monkeypatch):
    from shared_context.memory_bridge_settings import settings_key

    c = proposal_case
    install_settings(c)
    command = settings_command(c)
    target = settings_key(c.control.policy.authority_domain, command["request_id"])
    original, lost = c.h.store._submit, []

    def lose(operations, request):
        if request is not None and request.request_id == target and not lost:
            c.h.transport.after_apply = "lost"
            lost.append(True)
        try:
            return original(operations, request)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose)
    result = c.h.client.post(SETTINGS, json=command, headers=c.h.headers())
    assert result.status_code == 503 and result.json()["error"]["reconcile_original"] is True
    assert lost == [True]
    guard = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_args, **_kwargs: pytest.fail("receipt lookup cannot write"))
    lookup = c.h.client.post(SETTINGS + "/reconcile", json=command, headers=c.h.headers("read-only"))
    assert lookup.status_code == 200 and lookup.json()["result"]["settings_id"] == target
    assert c.h.store._guard() == guard


def test_selected_note_still_requires_its_current_source_and_original_agent_scope(proposal_case):
    from shared_context.memory_bridge_settings import read_memory_settings, resolve_settings_note
    from shared_context.behavior_reviewed_note_source import resolve_reviewed_note

    c = proposal_case
    install_settings(c)
    command = settings_command(c, recall=True)
    created = c.h.client.post(SETTINGS, json=command, headers=c.h.headers())
    assert created.status_code == 200
    record = read_memory_settings(c.h.store, c.control.policy, created.json()["result"]["settings_id"])
    result = c.h.store.consistent_read(lambda: resolve_settings_note(c.h.runtime, c.human, c.control.policy, record))
    assert result.value.source.claim_id == command["recall"]["claim_id"]
    destination = c.control.policy.template.model_copy(update={"agent_oid": "another-agent"})
    with pytest.raises(Problem) as mismatch:
        c.h.store.consistent_read(lambda: resolve_reviewed_note(
            c.h.runtime, c.human, c.binding.binding_id, c.binding.claim_ids[0], destination,
            c.control.policy.legacy_template_policy.purpose))
    assert mismatch.value.status == 403
    source = c.h.runtime.profiles[c.binding.binding_id].source
    withdrawal = c.h.client.post(
        "/behavior/v1/bindings/" + c.binding.binding_id + "/sources/" + source.source_id + "/withdraw",
        content=wire.SourceWithdrawal(
            request_id="withdraw-selected-memory", withdrawal_id="withdraw-selected-memory",
            reference=source, expected_access_revision=1,
        ).model_dump_json(), headers=c.h.headers())
    assert withdrawal.status_code == 200
    with pytest.raises(Problem):
        c.h.store.consistent_read(lambda: resolve_settings_note(c.h.runtime, c.human, c.control.policy, record))


def test_new_resolution_sees_a_reviewed_correction_without_rewriting_the_approved_setting(proposal_case):
    from shared_context.memory_bridge_settings import (
        capture_new_conversation_memory, read_memory_settings, resolve_settings_note,
    )
    from shared_context.behavior_reviewed_note_source import read_captured_note

    c = proposal_case
    install_settings(c)
    command = settings_command(c, recall=True)
    created = c.h.client.post(SETTINGS, json=command, headers=c.h.headers())
    assert created.status_code == 200
    identifier = created.json()["result"]["settings_id"]
    original = read_memory_settings(c.h.store, c.control.policy, identifier)
    captured = c.h.store.consistent_read(lambda: capture_new_conversation_memory(c.control, c.human))
    assert captured.value.recall.source == original.selected_source
    path = "/behavior/v1/bindings/" + c.binding.binding_id + "/claims/" + c.binding.claim_ids[0]
    prior = c.h.client.get(path, headers=c.h.headers()).json()
    source = c.h.runtime.profiles[c.binding.binding_id].source
    draft = wire.ClaimDraft(
        request_id="correct-selected-note", version=prior["reference"]["version"] + 1,
        text=prior["candidate"]["text"], supporting=(source,), effective_from=c.h.time,
        supersedes=prior["reference"]["sha256"],
    )
    preview = c.h.client.post(path + "/preview", content=draft.model_dump_json(), headers=c.h.headers())
    assert preview.status_code == 200
    review = wire.ClaimReview(
        **draft.model_dump(), decision_id="correct-selected-note",
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
        **{key: preview.json()[key] for key in ("candidate_sha256", "evidence_sha256", "policy_version")},
    )
    assert c.h.client.post(path + "/review", content=review.model_dump_json(), headers=c.h.headers()).status_code == 200
    assert c.h.client.post(path + "/commit", content=wire.Command(request_id=draft.request_id).model_dump_json(),
                           headers=c.h.headers()).status_code == 200
    resolved = c.h.store.consistent_read(lambda: resolve_settings_note(c.h.runtime, c.human, c.control.policy, original))
    assert resolved.value.source.claim_version == original.selected_source.claim_version + 1
    assert resolved.value.source.claim_sha256 != original.selected_source.claim_sha256
    assert read_memory_settings(c.h.store, c.control.policy, identifier) == original
    fresh = c.h.store.consistent_read(lambda: capture_new_conversation_memory(c.control, c.human))
    assert fresh.value.settings_id == captured.value.settings_id
    assert fresh.value.recall.source == resolved.value.source
    assert captured.value.recall.source == original.selected_source
    with pytest.raises(Problem) as changed:
        c.h.store.consistent_read(lambda: read_captured_note(
            c.h.runtime, c.human, captured.value.recall.origin_binding_id, captured.value.recall.source,
            c.control.policy.template, c.control.policy.legacy_template_policy.purpose))
    assert changed.value.body["error"]["code"] == "MEMORY_RECALL_CHANGED"
    latest = c.h.store.consistent_read(lambda: read_captured_note(
        c.h.runtime, c.human, fresh.value.recall.origin_binding_id, fresh.value.recall.source,
        c.control.policy.template, c.control.policy.legacy_template_policy.purpose))
    assert latest.value.source == fresh.value.recall.source


def settings_cli_environment(case, monkeypatch, *, response_lost=False, invalid_preview=False):
    from shared_context import behavior_memory_cli as cli

    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(
        token=case.h.headers()["Authorization"].removeprefix("Bearer "))
    requests = []

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        http = TestClient(case.h.app)
        original = http.stream

        @contextmanager
        def stream(method, path, **kwargs):
            requests.append((method, path))
            with original(method, path, **kwargs) as response:
                if response_lost and method == "POST" and path == SETTINGS:
                    response.read()
                    assert response.status_code == 200
                    raise httpx.ReadTimeout("PRIVATE_SETTINGS_TRANSPORT")
                if invalid_preview and path == SETTINGS + "/preview":
                    response.read()
                    assert response.status_code == 200
                    value = response.json()
                    value["note"]["text"] = 5
                    yield httpx.Response(200, json=value)
                    return
                yield response
        http.stream = stream
        return http

    monkeypatch.setattr(cli, "InteractiveBrowserCredential", lambda **_: credential)
    monkeypatch.setattr(cli.httpx, "Client", client)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "select-memory")
    return cli, credential, requests


def settings_cli_arguments(action, path=None):
    return [
        "--api", "https://poc.example.westus3.azurecontainerapps.io", "--tenant", TENANT,
        "--audience", "api://22222222-2222-2222-2222-222222222222", "--client-id", CLIENT_ID, action,
        *(["--request-file", str(path)] if path is not None else []),
    ]


@pytest.mark.parametrize("response_lost", [False, True])
def test_cli_explicit_settings_selection_is_once_and_reconcile_is_read_only(
        proposal_case, monkeypatch, tmp_path, capsys, response_lost):
    c = proposal_case
    install_settings(c)
    command = settings_command(c, recall=True)
    cli, credential, requests = settings_cli_environment(c, monkeypatch, response_lost=response_lost)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(command), encoding="utf-8")
    assert cli.main(settings_cli_arguments("select-memory-settings", path)) == (1 if response_lost else 0)
    marker = path.with_name(path.name + ".memory-settings.attempt.json")
    saved = marker.read_bytes()
    assert json.loads(saved)["command"]["recall"] == command["recall"]
    assert b"The accepted metrics source has been reviewed." not in saved
    before = list(requests)
    assert cli.main(settings_cli_arguments("select-memory-settings", path)) == 1
    assert requests == before
    path.unlink()
    credential.get_token.return_value = SimpleNamespace(
        token=c.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    guard = c.h.store._guard()
    assert cli.main(settings_cli_arguments("memory-settings-reconcile", path)) == 0
    assert cli.main(settings_cli_arguments("memory-settings")) == 0
    assert c.h.store._guard() == guard and marker.read_bytes() == saved
    assert requests == [("POST", SETTINGS + "/preview"), ("POST", SETTINGS),
                        ("POST", SETTINGS + "/reconcile"), ("GET", SETTINGS)]
    assert credential.authenticate.call_args_list[-1].kwargs["scopes"] == [
        "api://22222222-2222-2222-2222-222222222222/Behavior.Read"]
    output = capsys.readouterr()
    assert "PRIVATE_SETTINGS_TRANSPORT" not in output.out + output.err
    assert credential.get_token.return_value.token not in output.out + output.err


@pytest.mark.parametrize("interactive", [False, True])
def test_settings_cli_does_not_grant_scope_when_confirmation_is_missing(
        proposal_case, monkeypatch, tmp_path, interactive):
    c = proposal_case
    install_settings(c)
    command = settings_command(c)
    cli, credential, requests = settings_cli_environment(c, monkeypatch)
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda _: "cancel")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(command), encoding="utf-8")
    guard = c.h.store._guard()
    assert cli.main(settings_cli_arguments("select-memory-settings", path)) == 1
    assert c.h.store._guard() == guard
    assert not path.with_name(path.name + ".memory-settings.attempt.json").exists()
    assert requests == ([("POST", SETTINGS + "/preview")] if interactive else [])
    if not interactive:
        credential.authenticate.assert_not_called()


def test_malformed_setting_preview_is_not_printed_or_submitted(proposal_case, monkeypatch, tmp_path, capsys):
    c = proposal_case
    install_settings(c)
    command = settings_command(c, recall=True)
    cli, _, requests = settings_cli_environment(c, monkeypatch, invalid_preview=True)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("invalid preview cannot be confirmed"))
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(command), encoding="utf-8")
    guard = c.h.store._guard()
    assert cli.main(settings_cli_arguments("select-memory-settings", path)) == 1
    output = capsys.readouterr()
    assert output.out == "" and "MEMORY_NOTE_CLI_UNCONFIRMED" in output.err
    assert c.h.store._guard() == guard
    assert not path.with_name(path.name + ".memory-settings.attempt.json").exists()
    assert requests == [("POST", SETTINGS + "/preview")]


def test_production_application_exposes_settings_without_activating_any_memory(proposal_case):
    from shared_context.behavior_server import application

    c = proposal_case
    composition = SimpleNamespace(
        runtime=c.h.runtime, verifier=c.h.verifier, assistant=None, teams=None,
        installation=lambda: None, catalog=SimpleNamespace(require_activation=lambda: None),
        registered_actor=None, close=lambda: None, agent_id=None,
        instruction_ready=lambda *_: pytest.fail("settings lookup must not wake work"),
        launch_instruction=lambda *_: pytest.fail("settings lookup must not launch work"),
    )
    app = application(composition)
    paths = {route.path for route in app.routes}
    assert {SETTINGS, SETTINGS + "/preview", SETTINGS + "/reconcile"} <= paths
    guard = c.h.store._guard()
    client = TestClient(app)
    try:
        response = client.get(SETTINGS, headers=c.h.headers("read-only"))
        assert response.status_code == 200
        assert response.json()["settings_id"] is None and response.json()["enabled"] is False
    finally:
        client.close()
    assert c.h.store._guard() == guard and c.h.endpoint.requests == []
