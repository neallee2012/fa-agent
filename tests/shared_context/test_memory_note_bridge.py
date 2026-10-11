"""Existing allowlisted slots, exact proposal text and explicit human review/commit."""

from dataclasses import replace
from datetime import timedelta
import json
from types import MappingProxyType, SimpleNamespace
from contextlib import contextmanager
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import TENANT
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_operations import control_key
from shared_context.behavior_work_policy import WorkPolicyControl
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import CLIENT_ID, jwk_client, signing_key
from test_memory_note_proposals import install_case
from test_sdk_compaction_product import open_turn, ordinary, settle
from test_work_cost_policy import standing


@pytest.fixture
def bridge_case(assistant):
    h = assistant
    # This fixture configures an unused slot before admitting any work; no registration is rewritten.
    h.binding = h.binding.model_copy(update={"source_ids": (*h.binding.source_ids, "note-source")})
    h.runtime.bindings = MappingProxyType({**h.runtime.bindings, h.binding.binding_id: h.binding})
    h.policy = h.policy.model_copy(update={
        "binding_digest": h.runtime.binding_digest(h.binding),
        "human_text_enabled": True, "text_policy_version": "human-text-policy-v1",
    })
    h.mutate(runtime_key("policy", h.binding.binding_id),
             binding_digest=h.policy.binding_digest, human_text_enabled=True, text_policy_version="human-text-policy-v1")
    _, policy, observation, _, human = standing(h)
    control = WorkPolicyControl(h.runtime, policy, lambda: observation)
    control.install_offline(human, "note-bridge-configured-slot", h.time + timedelta(minutes=10))
    readiness = h.readiness()
    h.delivery.registration = replace(h.delivery.registration, verify_readiness=lambda: readiness)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    turn = open_turn(h, control, human, "note-bridge-source")
    settle(turn, ordinary(turn.assignment.sdk_session_id))
    path, proposal = install_case(turn)
    assert h.client.post(path, json=proposal, headers=h.headers("read-only")).status_code == 200
    claim_path = "/behavior/v1/bindings/" + turn.binding.binding_id + "/claims/" + turn.binding.claim_ids[0]
    original = h.client.get(claim_path, headers=h.headers())
    assert original.status_code == 200
    mapping = {
        "schema_version": "fa-note-bridge-v1", "request_id": "note-bridge",
        "source_id": "note-source", "claim_id": turn.binding.claim_ids[0],
        "claim_version": original.json()["reference"]["version"] + 1,
        "supersedes": original.json()["reference"]["sha256"],
        "effective_from": h.time.isoformat(),
    }
    try:
        yield SimpleNamespace(h=h, turn=turn, path=path + "/" + proposal["request_id"] + "/bridge",
                              mapping=mapping, claim_path=claim_path, original=original.json())
    finally:
        turn.client.close()


def test_exact_quote_needs_human_review_and_commit_before_it_is_current(bridge_case, monkeypatch):
    from shared_context import memory_note_proposal_api as api
    from shared_context.errors import Problem

    c = bridge_case
    errors = []
    original = api.run_in_threadpool

    async def observed(operation, *args):
        try:
            return await original(operation, *args)
        except Problem as error:
            errors.append(error.body["error"]["code"])
            raise

    monkeypatch.setattr(api, "run_in_threadpool", observed)
    source_guard = c.h.store._guard()
    ready = c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers())
    assert ready.status_code == 200, errors
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers())
    assert preview.status_code == 200
    assert preview.json()["text"] == "original inventory"
    assert preview.json()["source"]["source_id"] == "note-source"
    review = review_command(c.mapping, preview.json())
    assert c.h.client.post(c.path + "/review", json=review, headers=c.h.headers()).status_code == 200
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original
    result = c.h.client.post(c.path + "/commit", json=c.mapping, headers=c.h.headers())
    assert result.status_code == 200, result.text
    current = c.h.client.get(c.claim_path, headers=c.h.headers())
    assert current.status_code == 200
    assert current.json()["candidate"]["text"] == "original inventory"
    assert current.json()["reference"]["version"] == c.mapping["claim_version"]
    assert c.h.store._guard() != source_guard and c.h.endpoint.requests == []
    guard = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_args, **_kwargs: pytest.fail("replays must only read"))
    assert c.h.client.post(c.path + "/commit", json=c.mapping, headers=c.h.headers()).json() == result.json()
    reconcile = c.h.client.post(c.path + "/reconcile",
        json={"mapping": c.mapping, "stage": "commit"}, headers=c.h.headers("read-only"))
    assert reconcile.json() == result.json() and c.h.store._guard() == guard


def review_command(mapping, preview):
    return {
        "mapping": mapping,
        "reviewed_checks": ["necessity", "support", "contradiction", "sensitivity", "consent"],
        **{name: preview["review"][name] for name in ("candidate_sha256", "evidence_sha256", "policy_version")},
    }


@pytest.mark.parametrize("field", ["candidate_sha256", "evidence_sha256", "policy_version"])
def test_human_review_must_match_the_preview_it_confirmed(bridge_case, field):
    c = bridge_case
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers())
    assert preview.status_code == 200
    command = review_command(c.mapping, preview.json())
    command[field] = "other-review-policy" if field == "policy_version" else "0" * 64
    guard = c.h.store._guard()
    response = c.h.client.post(c.path + "/review", json=command, headers=c.h.headers())
    assert response.status_code == 409
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original


@pytest.mark.parametrize("change,status", [
    ({"source_id": "metrics"}, 409),
    ({"source_id": "unconfigured-source"}, 403),
    ({"claim_id": "unconfigured-claim"}, 403),
    ({"claim_version": 99}, 409),
    ({"supersedes": "0" * 64}, 409),
])
def test_unusable_mapping_stays_pending_without_changing_any_existing_source(bridge_case, change, status):
    c = bridge_case
    guard = c.h.store._guard()
    response = c.h.client.post(c.path + "/prepare", json={**c.mapping, **change}, headers=c.h.headers())
    assert response.status_code == status
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original


def test_mapping_response_loss_is_unknown_without_preparing_a_source_on_replay(bridge_case, monkeypatch):
    c = bridge_case
    from shared_context.memory_note_proposal_bridge import NoteBridgeCommand, NoteProposalBridge

    service = NoteProposalBridge(c.h.runtime)
    request = service._context(
        c.turn.binding.binding_id, "human-note-proposal",
        NoteBridgeCommand.model_validate_json(json.dumps(c.mapping)), c.turn.human,
    )[-1]
    submit, lost = c.h.store._submit, []

    def lose(operations, current):
        if current is not None and current.request_id == request.request_id and not lost:
            lost.append(True)
            c.h.transport.after_apply = "lost"
        try:
            return submit(operations, current)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose)
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 503
    assert lost == [True]
    guard = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_args, **_kwargs: pytest.fail("unknown cannot be resumed by lookup"))
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 503
    response = c.h.client.post(c.path + "/reconcile",
        json={"mapping": c.mapping, "stage": "prepare"}, headers=c.h.headers("read-only"))
    assert response.status_code == 503 and response.json()["error"]["reconcile_original"] is True
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original


def test_saved_source_response_loss_is_read_back_without_writing_again(bridge_case, monkeypatch):
    from shared_context.errors import Problem

    c = bridge_case
    original = c.h.runtime.commit_source

    def lost(*args, **kwargs):
        receipt = original(*args, **kwargs)
        if not kwargs.get("reconcile"):
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "synthetic lost response")
        return receipt

    monkeypatch.setattr(c.h.runtime, "commit_source", lost)
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 503
    guard = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_args, **_kwargs: pytest.fail("source receipt lookup cannot write"))
    readback = c.h.client.post(c.path + "/reconcile",
        json={"mapping": c.mapping, "stage": "prepare"}, headers=c.h.headers("read-only"))
    assert readback.status_code == 200 and readback.json()["state"] == "source_ready"
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original


def test_partial_human_review_is_never_completed_by_replay_or_lookup(bridge_case, monkeypatch):
    from shared_context.source_materials import SourceScope

    c = bridge_case
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers()).json()
    command = review_command(c.mapping, preview)
    scope = SourceScope.model_validate(preview["review"]["scope"])
    review_id = control_key("memory-note-bridge-review", preview["bridge_id"])
    target = control_key("source-human-review-request", scope.key, review_id)
    submit, lost = c.h.store._submit, []

    def lose(operations, current):
        if current is not None and current.request_id == target and not lost:
            lost.append(True)
            c.h.transport.after_apply = "lost"
        try:
            return submit(operations, current)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose)
    assert c.h.client.post(c.path + "/review", json=command, headers=c.h.headers()).status_code == 503
    assert lost == [True]
    guard = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_args, **_kwargs: pytest.fail("partial review cannot be completed again"))
    assert c.h.client.post(c.path + "/review", json=command, headers=c.h.headers()).status_code == 503
    lookup = {"mapping": c.mapping, "stage": "review", "review": command}
    assert c.h.client.post(c.path + "/reconcile", json=lookup, headers=c.h.headers("read-only")).status_code == 503
    changed = {**command, "evidence_sha256": "0" * 64}
    assert c.h.client.post(c.path + "/review", json=changed, headers=c.h.headers()).status_code == 409
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original


def bridge_cli(c, monkeypatch, *, loss=None):
    from shared_context import behavior_memory_cli as cli

    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(token=c.h.headers()["Authorization"].removeprefix("Bearer "))
    requests = []

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        http = TestClient(c.h.app)
        original_stream = http.stream

        @contextmanager
        def stream(method, path, **kwargs):
            requests.append((method, path))
            with original_stream(method, path, **kwargs) as response:
                if loss and path.endswith("/bridge/" + loss):
                    response.read()
                    assert response.status_code == 200
                    raise httpx.ReadTimeout("PRIVATE_BRIDGE_TRANSPORT")
                yield response

        http.stream = stream
        return http

    monkeypatch.setattr(cli, "InteractiveBrowserCredential", lambda **_: credential)
    monkeypatch.setattr(cli.httpx, "Client", client)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    return cli, credential, requests


def cli_arguments(c, stage, path, *, reconcile=False):
    return [
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", TENANT, "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", CLIENT_ID, "bridge", c.turn.binding.binding_id, "human-note-proposal",
        "--stage", stage, "--request-file", str(path),
        *(["--reconcile"] if reconcile else []),
    ]


def test_cli_human_chooses_each_stage_and_commits_only_the_reviewed_quote(
        bridge_case, monkeypatch, tmp_path, capsys):
    c = bridge_case
    cli, credential, requests = bridge_cli(c, monkeypatch)
    mapping_path, review_path = tmp_path / "mapping.json", tmp_path / "review.json"
    mapping_path.write_text(json.dumps(c.mapping), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "prepare-note")
    assert cli.main(cli_arguments(c, "prepare", mapping_path)) == 0
    capsys.readouterr()
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original

    assert cli.main(cli_arguments(c, "preview", mapping_path)) == 0
    preview = json.loads(capsys.readouterr().out)
    review_path.write_text(json.dumps(review_command(c.mapping, preview)), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "review-note")
    assert cli.main(cli_arguments(c, "review", review_path)) == 0
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original

    monkeypatch.setattr("builtins.input", lambda _: "commit-note")
    assert cli.main(cli_arguments(c, "commit", mapping_path)) == 0
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json()["candidate"]["text"] == "original inventory"
    marker = mapping_path.with_name(mapping_path.name + ".bridge-commit.attempt.json")
    saved = marker.read_bytes()
    assert b"original inventory" not in saved
    before = list(requests)
    assert cli.main(cli_arguments(c, "commit", mapping_path)) == 1
    assert requests == before
    mapping_path.unlink()
    guard = c.h.store._guard()
    credential.get_token.return_value = SimpleNamespace(
        token=c.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    assert cli.main(cli_arguments(c, "commit", mapping_path, reconcile=True)) == 0
    assert c.h.store._guard() == guard and marker.read_bytes() == saved
    assert requests.count(("POST", c.path + "/prepare")) == 1
    assert requests.count(("POST", c.path + "/review")) == 1
    assert requests.count(("POST", c.path + "/commit")) == 1
    assert credential.authenticate.call_args_list[-1].kwargs["scopes"] == [
        "api://22222222-2222-2222-2222-222222222222/Behavior.Read"]
    output = capsys.readouterr()
    assert credential.get_token.return_value.token not in output.out + output.err
    assert c.h.endpoint.requests == []


def test_cli_review_response_loss_only_reads_the_original_review(
        bridge_case, monkeypatch, tmp_path, capsys):
    c = bridge_case
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers()).json()
    command = review_command(c.mapping, preview)
    cli, credential, requests = bridge_cli(c, monkeypatch, loss="review")
    path = tmp_path / "review.json"
    path.write_text(json.dumps(command), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "review-note")
    assert cli.main(cli_arguments(c, "review", path)) == 1
    before = list(requests)
    assert cli.main(cli_arguments(c, "review", path)) == 1
    assert requests == before
    path.unlink()
    guard = c.h.store._guard()
    credential.get_token.return_value = SimpleNamespace(
        token=c.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    assert cli.main(cli_arguments(c, "review", path, reconcile=True)) == 0
    assert requests[-1] == ("POST", c.path + "/reconcile")
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original
    output = capsys.readouterr()
    assert "PRIVATE_BRIDGE_TRANSPORT" not in output.out + output.err
    assert credential.get_token.return_value.token not in output.out + output.err


@pytest.mark.parametrize("interactive", [False, True])
def test_cli_does_not_prepare_a_source_without_human_confirmation(
        bridge_case, monkeypatch, tmp_path, interactive):
    c = bridge_case
    cli, credential, requests = bridge_cli(c, monkeypatch)
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(c.mapping), encoding="utf-8")
    monkeypatch.setattr("sys.stdin.isatty", lambda: interactive)
    monkeypatch.setattr("builtins.input", lambda _: "cancel")
    guard = c.h.store._guard()
    assert cli.main(cli_arguments(c, "prepare", path)) == 1
    assert c.h.store._guard() == guard
    assert not path.with_name(path.name + ".bridge-prepare.attempt.json").exists()
    assert not any(method == "POST" for method, _ in requests)
    if not interactive:
        credential.authenticate.assert_not_called()


def test_incomplete_review_or_early_commit_does_not_publish_the_proposal(bridge_case):
    c = bridge_case
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers()).json()
    command = review_command(c.mapping, preview)
    command["reviewed_checks"].remove("consent")
    guard = c.h.store._guard()
    assert c.h.client.post(c.path + "/review", json=command, headers=c.h.headers()).status_code == 422
    assert c.h.client.post(c.path + "/commit", json=c.mapping, headers=c.h.headers()).status_code == 503
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original


def test_commit_after_another_claim_update_keeps_the_newer_head(bridge_case):
    from test_reviewed_work_memory import reviewed_note

    c = bridge_case
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers()).json()
    assert c.h.client.post(c.path + "/review", json=review_command(c.mapping, preview), headers=c.h.headers()).status_code == 200
    reviewed_note(c.h, version=c.mapping["claim_version"], text="Another explicitly reviewed update.",
                  request_id="other-reviewed-version", binding_id=c.turn.binding.binding_id)
    newer = c.h.client.get(c.claim_path, headers=c.h.headers()).json()
    response = c.h.client.post(c.path + "/commit", json=c.mapping, headers=c.h.headers())
    assert response.status_code == 409
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == newer
    guard = c.h.store._guard()
    assert c.h.client.post(c.path + "/reconcile",
        json={"mapping": c.mapping, "stage": "commit"}, headers=c.h.headers("read-only")).status_code == 409
    assert c.h.store._guard() == guard


def test_each_writing_stage_requires_current_human_approval_scope(bridge_case):
    c = bridge_case
    guard = c.h.store._guard()
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers("read-only")).status_code == 403
    assert c.h.store._guard() == guard
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers()).json()
    command = review_command(c.mapping, preview)
    guard = c.h.store._guard()
    assert c.h.client.post(c.path + "/review", json=command, headers=c.h.headers("read-only")).status_code == 403
    assert c.h.store._guard() == guard
    assert c.h.client.post(c.path + "/review", json=command, headers=c.h.headers()).status_code == 200
    guard = c.h.store._guard()
    assert c.h.client.post(c.path + "/commit", json=c.mapping, headers=c.h.headers("read-only")).status_code == 403
    assert c.h.store._guard() == guard
    assert c.h.client.get(c.claim_path, headers=c.h.headers()).json() == c.original


def test_current_read_revocation_blocks_even_historical_stage_lookup(bridge_case):
    c = bridge_case
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers()).json()
    command = review_command(c.mapping, preview)
    assert c.h.client.post(c.path + "/review", json=command, headers=c.h.headers()).status_code == 200
    revoked = c.h.client.post("/operator/v1/standing-work-policy/revoke",
        json={"decision_id": "revoke-bridge-read"}, headers=c.h.headers())
    assert revoked.status_code == 200
    guard = c.h.store._guard()
    response = c.h.client.post(c.path + "/reconcile",
        json={"mapping": c.mapping, "stage": "review", "review": command}, headers=c.h.headers("read-only"))
    assert response.status_code == 403 and c.h.store._guard() == guard


def test_applied_claim_commit_response_loss_is_only_reconciled(bridge_case, monkeypatch):
    from shared_context.memory_governance import ClaimReference

    c = bridge_case
    assert c.h.client.post(c.path + "/prepare", json=c.mapping, headers=c.h.headers()).status_code == 200
    preview = c.h.client.post(c.path + "/preview", json=c.mapping, headers=c.h.headers()).json()
    assert c.h.client.post(c.path + "/review",
        json=review_command(c.mapping, preview), headers=c.h.headers()).status_code == 200
    target = control_key("memory-review", ClaimReference.model_validate(preview["claim"]).key)
    submit, lost = c.h.store._submit, []

    def lose(operations, current):
        if current is not None and current.request_id == target and not lost:
            lost.append(True)
            c.h.transport.after_apply = "lost"
        try:
            return submit(operations, current)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose)
    assert c.h.client.post(c.path + "/commit", json=c.mapping, headers=c.h.headers()).status_code == 503
    assert lost == [True]
    current = c.h.client.get(c.claim_path, headers=c.h.headers()).json()
    assert current["candidate"]["text"] == "original inventory"
    guard = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_args, **_kwargs: pytest.fail("commit lookup cannot write"))
    receipt = c.h.client.post(c.path + "/reconcile",
        json={"mapping": c.mapping, "stage": "commit"}, headers=c.h.headers("read-only"))
    assert receipt.status_code == 200 and receipt.json()["result"]["claim"] == current["reference"]
    assert c.h.client.post(c.path + "/commit", json=c.mapping, headers=c.h.headers()).json() == receipt.json()
    assert c.h.store._guard() == guard
