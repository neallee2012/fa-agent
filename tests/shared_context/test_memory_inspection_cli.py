"""Read existing reviewed Team notes through the signed API, without memory writes."""

import importlib
import json
from contextlib import contextmanager
from datetime import timedelta
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from azure.core.exceptions import ClientAuthenticationError
from fastapi.testclient import TestClient
import httpx
import pytest

from conftest import A, TENANT
from shared_context import behavior_wire as wire
from shared_context.behavior_runtime import runtime_key
from shared_context.memory_governance import ReviewedClaim, claim_digest
from test_behavior_api import METRICS
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token
from test_operation_rejection_cli import cold_cli
from test_reviewed_work_memory import NOTE, reviewed_note


@pytest.fixture
def note_case(assistant):
    candidate = reviewed_note(assistant)
    return SimpleNamespace(h=assistant, candidate=candidate)


def arguments(case, *, claim_id="claim-one"):
    return [
        "--api", "https://poc.example.westus3.azurecontainerapps.io",
        "--tenant", TENANT,
        "--audience", "api://22222222-2222-2222-2222-222222222222",
        "--client-id", CLIENT_ID,
        "show", case.h.binding.binding_id, claim_id,
    ]


def operator_environment(case, monkeypatch, *, transform=None, transport_failure=False):
    cli = importlib.import_module("shared_context.behavior_memory_cli")
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(
        token=case.h.headers("read-only")["Authorization"].removeprefix("Bearer "))
    requests, attempts = [], []

    def client(**options):
        assert options["trust_env"] is False and options["follow_redirects"] is False
        http = TestClient(case.h.app)
        http.event_hooks["request"].append(lambda request: requests.append((request.method, request.url.path)))
        stream = http.stream

        @contextmanager
        def observed(method, path, **kwargs):
            assert method == "GET" and kwargs["follow_redirects"] is False
            attempts.append((method, path))
            if transport_failure:
                raise httpx.ReadTimeout("PRIVATE_MEMORY_TRANSPORT_DIAGNOSTIC")
            with stream(method, path, **kwargs) as response:
                if transform is None:
                    yield response
                else:
                    response.read()
                    yield transform(response)

        http.stream = observed
        return http

    monkeypatch.setattr(cli, "InteractiveBrowserCredential", lambda **options: credential)
    monkeypatch.setattr(cli.httpx, "Client", client)
    return cli, credential, requests, attempts


def test_operator_reads_the_current_reviewed_note_without_writes_or_private_review_metadata(
        note_case, monkeypatch, capsys):
    c = note_case
    cli, credential, requests, attempts = operator_environment(c, monkeypatch)
    before, batches = c.h.store._guard(), len(c.h.transport.parts)

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only memory inspection cannot create control records or start a runtime")

    monkeypatch.setattr(c.h.store, "transact", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    assert cli.main(arguments(c)) == 0
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value["usage"] == "knowledge-data-not-permission"
    assert value["binding_id"] == c.h.binding.binding_id
    assert value["reference"]["claim_id"] == "claim-one" and value["reference"]["version"] == 2
    assert value["team_id"] == A.team and value["kind"] == "note"
    assert value["text"] == NOTE and "TEAM_MEMORY_CANARY" in output.out
    assert value["human_reviewed"] is True
    assert value["expires_at"] is None
    assert value["supporting"] == [c.h.runtime.profiles[c.h.binding.binding_id].source.model_dump(mode="json")]
    assert "reviewer_id" not in output.out and "read_set" not in output.out
    assert HUMAN_OID not in output.out and A.oid not in output.out
    assert credential.get_token.return_value.token not in output.out + output.err
    assert output.err == ""
    assert requests == [("GET", "/behavior/v1/bindings/binding-one/claims/claim-one")]
    assert attempts == requests
    credential.authenticate.assert_called_once_with(
        scopes=["api://22222222-2222-2222-2222-222222222222/Behavior.Read"])
    assert c.h.store._guard() == before and len(c.h.transport.parts) == batches
    assert c.h.endpoint.requests == []


@pytest.mark.parametrize("kind", ["profile", "episode"])
def test_cli_inspects_reviewed_types_and_validity_without_new_write_authority(note_case, monkeypatch, capsys, kind):
    c = note_case
    expires = c.h.time + timedelta(minutes=10)
    reviewed_note(c.h, version=3, request_id="typed-cli", kind=kind, expires_at=expires)
    cli, _, requests, _ = operator_environment(c, monkeypatch)
    before = c.h.store._guard()
    assert cli.main(arguments(c)) == 0
    output = capsys.readouterr()
    value = json.loads(output.out)
    assert value["kind"] == kind and value["text"] == NOTE
    assert value["reference"]["version"] == 3 and value["expires_at"] is not None
    assert value["usage"] == "knowledge-data-not-permission" and output.err == ""
    assert c.h.store._guard() == before and all(method == "GET" for method, _ in requests)


def test_explicit_sources_reads_exact_direct_reference_and_rechecks_the_note_before_output(
        note_case, monkeypatch, capsys):
    c = note_case
    cli, credential, requests, attempts = operator_environment(c, monkeypatch)
    before, batches = c.h.store._guard(), len(c.h.transport.parts)

    def forbidden(*args, **kwargs):
        pytest.fail("Inspecting original sources cannot write records or start a runtime")

    monkeypatch.setattr(c.h.store, "transact", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    assert cli.main([*arguments(c), "--with-sources"]) == 0
    output = capsys.readouterr()
    value = json.loads(output.out)
    reference = c.h.runtime.profiles[c.h.binding.binding_id].source.model_dump(mode="json")
    assert value["text"] == NOTE
    assert value["originals"] == [{"source": reference, "text": METRICS.decode("utf-8")}]
    assert value["usage"] == "knowledge-data-not-permission" and output.err == ""
    assert requests == attempts == [
        ("GET", "/behavior/v1/bindings/binding-one/claims/claim-one"),
        ("GET", "/behavior/v1/bindings/binding-one/sources/metrics/versions/1"),
        ("GET", "/behavior/v1/bindings/binding-one/claims/claim-one"),
    ]
    credential.authenticate.assert_called_once_with(
        scopes=["api://22222222-2222-2222-2222-222222222222/Behavior.Read"])
    assert c.h.store._guard() == before and len(c.h.transport.parts) == batches
    assert c.h.endpoint.requests == []


@pytest.mark.parametrize("fault,code", [
    ("version", "MEMORY_NOTE_SOURCE_INVALID"),
    ("header-hash", "MEMORY_NOTE_SOURCE_INVALID"),
    ("body-hash", "MEMORY_NOTE_SOURCE_INVALID"),
    ("media-type", "MEMORY_NOTE_SOURCE_INVALID"),
    ("partial-status", "MEMORY_NOTE_SOURCE_UNAVAILABLE"),
    ("partial-stream", "MEMORY_NOTE_SOURCE_UNKNOWN"),
])
def test_source_failures_do_not_print_the_note_or_any_original(note_case, monkeypatch, capsys, fault, code):
    c = note_case

    class Partial(httpx.SyncByteStream):
        def __iter__(self):
            yield METRICS[:10]
            raise httpx.ReadError("PRIVATE_SOURCE_STREAM_DETAIL")

    def altered(response):
        if "/sources/" not in response.request.url.path:
            return response
        assert response.status_code == 200
        headers = dict(response.headers)
        if fault == "version":
            headers["x-source-version"] = "2"
        elif fault == "header-hash":
            headers["x-source-sha256"] = "0" * 64
        elif fault == "media-type":
            headers["content-type"] = "application/octet-stream"
        elif fault == "partial-stream":
            return httpx.Response(200, headers=headers, stream=Partial())
        body = b"X" + response.content[1:] if fault == "body-hash" else response.content
        return httpx.Response(206 if fault == "partial-status" else 200, headers=headers, content=body)

    cli, _, requests, attempts = operator_environment(c, monkeypatch, transform=altered)
    before = c.h.store._guard()
    assert cli.main([*arguments(c), "--with-sources"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error"]["code"] == code
    assert NOTE not in output.err and "PRIVATE_SOURCE_STREAM_DETAIL" not in output.err
    assert c.h.store._guard() == before
    assert requests == attempts == [
        ("GET", "/behavior/v1/bindings/binding-one/claims/claim-one"),
        ("GET", "/behavior/v1/bindings/binding-one/sources/metrics/versions/1"),
    ]


def test_source_byte_length_must_match_even_when_its_hash_and_text_are_correct(note_case, monkeypatch, capsys):
    c = note_case

    def altered(response):
        if "/claims/" not in response.request.url.path:
            return response
        assert response.status_code == 200
        payload = response.json()
        payload["candidate"]["supporting"][0]["byte_length"] += 1
        parsed = ReviewedClaim.model_validate_json(json.dumps(payload))
        payload["reference"]["sha256"] = claim_digest(parsed.candidate, parsed.reference.scope)
        payload["review"]["candidate_sha256"] = payload["reference"]["sha256"]
        return httpx.Response(200, json=payload)

    cli, _, requests, _ = operator_environment(c, monkeypatch, transform=altered)
    assert cli.main([*arguments(c), "--with-sources"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error"]["code"] == "MEMORY_NOTE_SOURCE_INVALID"
    assert len(requests) == 2


@pytest.mark.parametrize("change", ["note-correction", "source-withdrawal"])
def test_a_change_after_source_read_is_rejected_before_any_output(note_case, monkeypatch, capsys, change):
    c = note_case
    injected = []

    def changed(response):
        if "/sources/" in response.request.url.path:
            assert response.status_code == 200 and not injected
            if change == "note-correction":
                reviewed_note(c.h, text="New reviewed note.", version=3, request_id="during-source-read")
            else:
                source = c.h.runtime.profiles[c.h.binding.binding_id].source
                withdrawn = c.h.post("/sources/" + source.source_id + "/withdraw", wire.SourceWithdrawal(
                    request_id="withdraw-during-source-read", withdrawal_id="withdraw-during-source-read",
                    reference=source, expected_access_revision=1,
                ))
                assert withdrawn.status_code == 200
            injected.append(c.h.store._guard())
        return response

    cli, _, requests, attempts = operator_environment(c, monkeypatch, transform=changed)
    assert cli.main([*arguments(c), "--with-sources"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    expected = "MEMORY_NOTE_CHANGED" if change == "note-correction" else "MEMORY_NOTE_UNAVAILABLE"
    assert json.loads(output.err)["error"]["code"] == expected
    assert c.h.store._guard() == injected[0]
    assert requests == attempts == [
        ("GET", "/behavior/v1/bindings/binding-one/claims/claim-one"),
        ("GET", "/behavior/v1/bindings/binding-one/sources/metrics/versions/1"),
        ("GET", "/behavior/v1/bindings/binding-one/claims/claim-one"),
    ]


def test_a_later_reference_failure_does_not_print_an_already_read_source(note_case, monkeypatch, capsys):
    c = note_case

    def extra_reference(response):
        if "/claims/" not in response.request.url.path:
            return response
        assert response.status_code == 200
        payload = response.json()
        reference = dict(payload["candidate"]["supporting"][0])
        reference["source_id"] = "unconfigured-source"
        payload["candidate"]["supporting"].append(reference)
        parsed = ReviewedClaim.model_validate_json(json.dumps(payload))
        payload["reference"]["sha256"] = claim_digest(parsed.candidate, parsed.reference.scope)
        payload["review"]["candidate_sha256"] = payload["reference"]["sha256"]
        return httpx.Response(200, json=payload)

    cli, _, requests, attempts = operator_environment(c, monkeypatch, transform=extra_reference)
    before = c.h.store._guard()
    assert cli.main([*arguments(c), "--with-sources"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error"]["code"] == "MEMORY_NOTE_SOURCE_UNAVAILABLE"
    assert METRICS.decode("utf-8") not in output.err
    assert c.h.store._guard() == before
    assert requests == attempts == [
        ("GET", "/behavior/v1/bindings/binding-one/claims/claim-one"),
        ("GET", "/behavior/v1/bindings/binding-one/sources/metrics/versions/1"),
        ("GET", "/behavior/v1/bindings/binding-one/sources/unconfigured-source/versions/1"),
    ]


def test_a_new_reviewed_version_replaces_the_view_without_returning_cached_text(note_case, monkeypatch, capsys):
    c = note_case
    cli, _, requests, _ = operator_environment(c, monkeypatch)
    assert cli.main(arguments(c)) == 0
    assert json.loads(capsys.readouterr().out)["text"] == NOTE
    corrected = "Reviewed correction: check the latest original receipt."
    reviewed_note(c.h, text=corrected, version=3, request_id="corrected-note")
    before = c.h.store._guard()
    assert cli.main(arguments(c)) == 0
    output = capsys.readouterr()
    current = json.loads(output.out)
    assert current["text"] == corrected and current["reference"]["version"] == 3
    assert NOTE not in output.out
    assert c.h.store._guard() == before
    assert requests == [("GET", "/behavior/v1/bindings/binding-one/claims/claim-one")] * 2


def test_a_reviewed_but_uncommitted_proposal_does_not_replace_the_current_note(note_case, monkeypatch, capsys):
    c = note_case
    reviewed_note(c.h, text="UNCOMMITTED_NOTE_CANARY", version=3, request_id="uncommitted-note", commit=False)
    cli, _, requests, _ = operator_environment(c, monkeypatch)
    before = c.h.store._guard()
    assert cli.main(arguments(c)) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["text"] == NOTE
    assert "UNCOMMITTED_NOTE_CANARY" not in output.out
    assert c.h.store._guard() == before and len(requests) == 1


@pytest.mark.parametrize("change,status", [("source-withdrawn", 403), ("expired", 409), ("read-revoked", 403)])
def test_current_authority_refuses_unavailable_notes_without_reusing_a_previous_result(
        note_case, monkeypatch, capsys, change, status):
    c = note_case
    cli, _, requests, _ = operator_environment(c, monkeypatch)
    assert cli.main(arguments(c)) == 0
    capsys.readouterr()
    if change == "source-withdrawn":
        source = c.h.runtime.profiles[c.h.binding.binding_id].source
        response = c.h.post("/sources/" + source.source_id + "/withdraw", wire.SourceWithdrawal(
            request_id="withdraw-note-source", withdrawal_id="withdraw-note-source",
            reference=source, expected_access_revision=1,
        ))
        assert response.status_code == 200
    elif change == "expired":
        reviewed_note(c.h, version=3, request_id="expiring-note", expires_at=c.h.time + timedelta(seconds=2))
        c.h.time += timedelta(seconds=3)
    else:
        c.h.mutate(runtime_key("policy", c.h.binding.binding_id), active=False)
    before, batches = c.h.store._guard(), len(c.h.transport.parts)
    assert cli.main(arguments(c)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["upstream_status"] == status
    assert NOTE not in output.err
    assert c.h.store._guard() == before and len(c.h.transport.parts) == batches
    assert len(requests) == 2 and c.h.endpoint.requests == []


@pytest.mark.parametrize("identity", ["wrong-human", "old-scope"])
def test_cli_requested_scope_does_not_override_the_authenticated_reader(note_case, monkeypatch, capsys, identity):
    c = note_case
    cli, credential, requests, _ = operator_environment(c, monkeypatch)
    if identity == "wrong-human":
        bearer = token(c.h.signing, {"oid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "scp": "Behavior.Read"})
    else:
        bearer = c.h.headers("old-m3")["Authorization"].removeprefix("Bearer ")
    credential.get_token.return_value = SimpleNamespace(token=bearer)
    before = c.h.store._guard()
    assert cli.main(arguments(c)) == 1
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error"]["upstream_status"] == 403
    assert bearer not in output.err and c.h.store._guard() == before
    assert len(requests) == 1


def test_an_unconfigured_claim_id_cannot_select_another_note(note_case, monkeypatch, capsys):
    c = note_case
    cli, _, requests, _ = operator_environment(c, monkeypatch)
    before = c.h.store._guard()
    assert cli.main(arguments(c, claim_id="unconfigured-claim")) == 1
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error"]["upstream_status"] == 403
    assert c.h.store._guard() == before
    assert requests == [("GET", "/behavior/v1/bindings/binding-one/claims/unconfigured-claim")]


@pytest.mark.parametrize("change", [
    "missing-schema", "boolean-schema", "future-schema", "claim-id", "text", "review-hash",
    "nonhuman-review", "missing-check", "private-scope", "candidate-private", "scope-private",
    "foreign-tenant", "unknown-kind", "reference-version", "reference-subject", "support-scope",
])
def test_inconsistent_claim_responses_are_not_partially_printed(note_case, monkeypatch, capsys, change):
    c = note_case

    def altered(response):
        assert response.status_code == 200
        payload = response.json()
        if change == "missing-schema":
            payload.pop("schema_version")
        elif change == "boolean-schema":
            payload["schema_version"] = True
        elif change == "future-schema":
            payload["schema_version"] = 999
        elif change == "claim-id":
            payload["reference"]["claim_id"] = "other-claim"
            payload["candidate"]["claim_id"] = "other-claim"
        elif change == "text":
            payload["candidate"]["text"] = "PRIVATE_UNVERIFIED_MEMORY_CONTENT"
        elif change == "review-hash":
            payload["review"]["candidate_sha256"] = "0" * 64
        elif change == "nonhuman-review":
            payload["review"]["reviewer_kind"] = "independent-model"
        elif change == "missing-check":
            payload["review"]["reviewed_checks"].pop()
        elif change == "private-scope":
            payload["reference"]["scope"].update(visibility="private", owner_actor="private-owner")
            payload["candidate"]["visibility"] = "private"
            for source in payload["candidate"]["supporting"]:
                source["scope"] = dict(payload["reference"]["scope"])
        elif change == "candidate-private":
            payload["candidate"]["visibility"] = "private"
        elif change == "scope-private":
            payload["reference"]["scope"].update(visibility="private", owner_actor="private-owner")
            for source in payload["candidate"]["supporting"]:
                source["scope"] = dict(payload["reference"]["scope"])
        elif change == "foreign-tenant":
            payload["reference"]["scope"]["tenant_id"] = "other-tenant"
            for source in payload["candidate"]["supporting"]:
                source["scope"] = dict(payload["reference"]["scope"])
        elif change == "unknown-kind":
            payload["candidate"]["kind"] = "policy"
        elif change == "reference-version":
            payload["reference"]["version"] += 1
        elif change == "reference-subject":
            payload["reference"]["subject"] = "other-subject"
        elif change == "support-scope":
            payload["candidate"]["supporting"][0]["scope"]["team_id"] = "other-team"
        if change in {
            "claim-id", "private-scope", "candidate-private", "scope-private", "foreign-tenant",
            "reference-version", "reference-subject", "support-scope",
        }:
            # Keep digest checks valid so they cannot mask the intended scope or correlation refusal.
            parsed = ReviewedClaim.model_validate_json(json.dumps(payload))
            payload["reference"]["sha256"] = claim_digest(parsed.candidate, parsed.reference.scope)
            payload["review"]["candidate_sha256"] = payload["reference"]["sha256"]
            checked = ReviewedClaim.model_validate_json(json.dumps(payload))
            assert checked.reference.sha256 == claim_digest(checked.candidate, checked.reference.scope)
            assert checked.review.candidate_sha256 == checked.reference.sha256
        return httpx.Response(200, json=payload)

    cli, credential, requests, _ = operator_environment(c, monkeypatch, transform=altered)
    before = c.h.store._guard()
    assert cli.main(arguments(c)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["code"] == "MEMORY_NOTE_CLI_UNCONFIRMED"
    assert "PRIVATE_UNVERIFIED_MEMORY_CONTENT" not in output.err
    assert NOTE not in output.err and credential.get_token.return_value.token not in output.err
    assert c.h.store._guard() == before and len(requests) == 1


@pytest.mark.parametrize("response_kind", ["duplicate-key", "invalid-json", "oversized"])
def test_invalid_or_oversized_json_never_becomes_a_partial_note(note_case, monkeypatch, capsys, response_kind):
    c = note_case

    def altered(response):
        assert response.status_code == 200
        if response_kind == "duplicate-key":
            raw = b'{"schema_version":1,"schema_version":1,"private":"PRIVATE_MEMORY_RESPONSE"}'
        elif response_kind == "invalid-json":
            raw = b'{"private":"PRIVATE_MEMORY_RESPONSE"'
        else:
            raw = b" " * (128 * 1024 + 1 - len(response.content)) + response.content
            assert len(raw) == 128 * 1024 + 1
            assert json.loads(raw) == response.json()
        return httpx.Response(200, content=raw)

    cli, _, requests, _ = operator_environment(c, monkeypatch, transform=altered)
    assert cli.main(arguments(c)) == 1
    output = capsys.readouterr()
    assert output.out == "" and "PRIVATE_MEMORY_RESPONSE" not in output.err
    assert json.loads(output.err)["error"]["code"] == "MEMORY_NOTE_RESPONSE_INVALID"
    assert len(requests) == 1


def test_exact_response_byte_limit_accepts_a_complete_valid_note(note_case, monkeypatch, capsys):
    c = note_case

    def padded(response):
        assert response.status_code == 200 and len(response.content) < 128 * 1024
        raw = b" " * (128 * 1024 - len(response.content)) + response.content
        assert len(raw) == 128 * 1024
        assert json.loads(raw) == response.json()
        return httpx.Response(200, content=raw)

    cli, _, requests, attempts = operator_environment(c, monkeypatch, transform=padded)
    before = c.h.store._guard()
    assert cli.main(arguments(c)) == 0
    assert json.loads(capsys.readouterr().out)["text"] == NOTE
    assert c.h.store._guard() == before
    assert requests == attempts == [("GET", "/behavior/v1/bindings/binding-one/claims/claim-one")]


def test_transport_failure_is_unknown_without_returning_cached_note_or_repeating_get(note_case, monkeypatch, capsys):
    c = note_case
    cli, _, requests, attempts = operator_environment(c, monkeypatch, transport_failure=True)
    before = c.h.store._guard()
    assert cli.main(arguments(c)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["error"]["code"] == "MEMORY_NOTE_READ_UNKNOWN"
    assert "PRIVATE_MEMORY_TRANSPORT_DIAGNOSTIC" not in output.err
    assert requests == [] and c.h.store._guard() == before
    assert attempts == [("GET", "/behavior/v1/bindings/binding-one/claims/claim-one")]


@pytest.mark.parametrize("phase", ["authenticate", "get-token"])
def test_authentication_diagnostics_do_not_leak_note_or_credential_text(
        note_case, monkeypatch, capsys, caplog, phase):
    c = note_case
    cli, credential, requests, _ = operator_environment(c, monkeypatch)

    def failed(*args, **kwargs):
        logging.getLogger("azure.identity").warning("PRIVATE_MEMORY_CREDENTIAL_DETAIL")
        raise ClientAuthenticationError("PRIVATE_MEMORY_CREDENTIAL_DETAIL")

    if phase == "authenticate":
        credential.authenticate.side_effect = failed
    else:
        credential.get_token.side_effect = failed
    assert cli.main(arguments(c)) == 1
    output = capsys.readouterr()
    assert output.out == "" and not requests
    assert json.loads(output.err)["error"]["code"] == "MEMORY_NOTE_AUTHENTICATION_FAILED"
    assert "PRIVATE_MEMORY_CREDENTIAL_DETAIL" not in output.err + caplog.text
    assert NOTE not in output.err


@pytest.mark.parametrize("field,value", [
    ("--api", "https://unconfigured.invalid"),
    ("--tenant", "not-a-tenant"),
    ("binding", "../binding-one"),
    ("claim_id", "claim-one?scope=other"),
])
def test_invalid_connection_or_identifiers_are_rejected_before_authentication(
        note_case, monkeypatch, capsys, field, value):
    cli, credential, requests, _ = operator_environment(note_case, monkeypatch)
    args = arguments(note_case)
    index = -2 if field == "binding" else -1 if field == "claim_id" else args.index(field) + 1
    args[index] = value
    assert cli.main(args) == 1
    assert capsys.readouterr().out == "" and not requests
    credential.authenticate.assert_not_called()


def test_memory_inspection_cli_help_loads_without_the_hosted_sdk():
    result = cold_cli(
        "import runpy,sys; sys.modules['copilot']=None; "
        "sys.argv=['behavior_memory_cli','--help']; "
        "runpy.run_module('shared_context.behavior_memory_cli',run_name='__main__')"
    )
    assert result.returncode == 0, result.stderr
    assert "{show,candidate,propose,proposal-reconcile,proposal,bridge,memory-settings,select-memory-settings,memory-settings-reconcile}" in result.stdout
    assert result.stderr == ""
