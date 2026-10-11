"""Human CLI uses a distinct synthesis route/attempt without impersonating review."""

import json

import pytest

from conftest import TENANT
from shared_context import behavior_memory_cli as cli
from shared_context.errors import Problem
from shared_context.memory_bridge_settings import memory_slot_ids
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import CLIENT_ID, jwk_client, signing_key
from test_memory_note_bridge import review_command
from test_memory_note_proposal_cli import environment
from test_memory_note_synthesis import synthesis_case


def args(case, action, path=None, stage=None, reconcile=False):
    result = [
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", TENANT, "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", CLIENT_ID, action, case.binding.binding_id, "--synthesis",
    ]
    if action in {"proposal", "bridge"}:
        result.append(case.command["request_id"])
    if path is not None:
        result.extend(("--request-file", str(path)))
    if stage is not None:
        result.extend(("--stage", stage))
    if reconcile:
        result.append("--reconcile")
    return result


@pytest.mark.parametrize("lost", [False, True])
def test_synthesis_cli_sends_once_and_reconciles_only_its_original_family(
        synthesis_case, monkeypatch, tmp_path, capsys, lost):
    c = synthesis_case
    credential, calls = environment(c, monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "share-synthesis")
    original = cli._request

    def request(http, token, method, path, body=None):
        value = original(http, token, method, path, body)
        if lost and method == "POST" and path == c.path:
            raise Problem(503, "UNRESOLVED_CLI", "synthetic response loss after the original synthesis")
        return value

    monkeypatch.setattr(cli, "_request", request)
    file = tmp_path / "synthesis.json"
    file.write_text(json.dumps(c.command), encoding="utf-8")
    assert cli.main(args(c, "propose", file)) == (1 if lost else 0)
    attempt = file.with_name(file.name + ".synthesis.attempt.json")
    raw = attempt.read_bytes()
    assert json.loads(raw)["schema_version"] == "fa-note-synthesis-cli-attempt-v1"
    assert b"ships only" not in raw
    assert cli.main(args(c, "propose", file)) == 1
    file.unlink()
    assert cli.main(args(c, "proposal-reconcile", file)) == 0
    assert cli.main(args(c, "proposal")) == 0
    assert attempt.read_bytes() == raw
    assert calls == [("POST", c.path + "/preview"), ("POST", c.path),
                     ("POST", c.path + "/reconcile"), ("GET", c.path + "/" + c.command["request_id"])]
    out = capsys.readouterr()
    assert credential.get_token.return_value.token not in out.out + out.err


def test_synthesis_cli_displays_originals_and_uses_the_same_human_bridge(synthesis_case, monkeypatch, tmp_path):
    c = synthesis_case
    assert c.h.client.post(c.path, json=c.command, headers=c.h.headers("read-only")).status_code == 200
    credential, calls = environment(c, monkeypatch)
    credential.get_token.return_value.token = c.h.headers()["Authorization"].removeprefix("Bearer ")
    confirmations = iter(("prepare-note", "review-note", "commit-note"))
    monkeypatch.setattr("builtins.input", lambda _: next(confirmations))
    source, claim = memory_slot_ids(c.control.records[c.binding.binding_id].admission_id)
    mapping = {
        "schema_version": "fa-note-bridge-v1", "request_id": "cli-synthesis-bridge",
        "source_id": source, "claim_id": claim, "claim_version": 1,
        "supersedes": None, "effective_from": c.h.time.isoformat(),
    }
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    assert cli.main(args(c, "bridge", path, "prepare")) == 0
    assert path.with_name(path.name + ".synthesis-bridge-prepare.attempt.json").exists()
    assert cli.main(args(c, "bridge", path, "preview")) == 0
    preview = c.h.client.post(c.path + "/" + c.command["request_id"] + "/bridge/preview",
                             json=mapping, headers=c.h.headers()).json()
    review_file = tmp_path / "review.json"
    review_file.write_text(json.dumps(review_command(mapping, preview)), encoding="utf-8")
    assert cli.main(args(c, "bridge", review_file, "review")) == 0
    assert cli.main(args(c, "bridge", path, "commit")) == 0
    assert cli.main(args(c, "bridge", path, "commit", reconcile=True)) == 0
    assert all("/note-syntheses/" in route for _, route in calls)
