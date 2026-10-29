from __future__ import annotations

import hashlib
import os
from unittest.mock import patch

import httpx
import pytest

from shared_context.errors import Problem
from shared_context.m3_client import ArtifactClient
from shared_context.m3_models import ApprovalInput
from test_m3_api import client
from test_m3_artifacts import HUMAN, V1, V2, create, m3, upload


def test_cli_download_is_verified_complete_and_never_overwrites_user_files(m3, tmp_path):
    service, _, _ = m3
    create(service)
    upload(service)
    consumer = ArtifactClient(client(service, HUMAN), HUMAN.team, lambda: "synthetic-token")
    output = tmp_path / "delivered.txt"
    version = consumer.download("report", V1, output)
    assert output.read_bytes() == b"report v1\n"
    assert version.sha256 == hashlib.sha256(output.read_bytes()).hexdigest()
    output.write_bytes(b"user modification")
    with patch.object(consumer.http, "stream", wraps=consumer.http.stream) as request:
        with pytest.raises(FileExistsError):
            consumer.download("report", V1, output)
    request.assert_not_called()
    assert output.read_bytes() == b"user modification"
    assert list(tmp_path.glob(".fa-m3-download-*")) == []


def test_racing_download_destination_preserves_existing_data(m3, tmp_path):
    service, _, _ = m3
    create(service)
    upload(service)
    consumer = ArtifactClient(client(service, HUMAN), HUMAN.team, lambda: "synthetic-token")
    destination = tmp_path / "racing.txt"
    native_link = os.link

    def race(source, target):
        destination.write_bytes(b"racing user file")
        native_link(source, target)

    with patch("shared_context.m3_client.os.link", side_effect=race), pytest.raises(FileExistsError):
        consumer.download("report", V1, destination)
    assert destination.read_bytes() == b"racing user file"
    assert list(tmp_path.glob(".fa-m3-download-*")) == []


def test_download_integrity_error_does_not_create_output(m3, tmp_path):
    service, _, _ = m3
    create(service)
    upload(service)
    consumer = ArtifactClient(client(service, HUMAN), HUMAN.team, lambda: "synthetic-token")
    native_request = consumer._request

    def corrupt(method, path, **kwargs):
        headers, data = native_request(method, path, **kwargs)
        return (headers, b"wrong bytes") if kwargs.get("binary") else (headers, data)

    with patch.object(consumer, "_request", side_effect=corrupt), pytest.raises(Problem):
        consumer.download("report", V1, tmp_path / "bad.txt")
    assert list(tmp_path.iterdir()) == []


def test_cli_submits_an_exact_decision_once_and_checks_receipt(m3):
    service, _, _ = m3
    create(service, "sop")
    upload(service)
    consumer = ArtifactClient(client(service, HUMAN), HUMAN.team, lambda: "synthetic-token")
    snapshot = consumer.get("report")
    version = consumer.version(snapshot, V1)
    body = ApprovalInput(decision_id=V1, version_id=V1, decision="approve", sha256=version.sha256)
    with patch.object(consumer.http, "stream", wraps=consumer.http.stream) as requests:
        result = consumer.decide("report", snapshot, body)
    assert requests.call_count == 1
    assert result.payload["effective_version_id"] == V1


def test_cli_does_not_retry_an_unknown_decision(m3):
    service, _, _ = m3
    create(service, "sop")
    upload(service)
    consumer = ArtifactClient(client(service, HUMAN), HUMAN.team, lambda: "synthetic-token")
    snapshot = consumer.get("report")
    body = ApprovalInput(decision_id=V1, version_id=V1, decision="approve", sha256=consumer.version(snapshot, V1).sha256)
    with patch.object(consumer.http, "stream", side_effect=httpx.ReadTimeout("lost response")) as requests:
        with pytest.raises(Problem) as exc:
            consumer.decide("report", snapshot, body)
    assert exc.value.body["error"]["code"] == "M3_REQUEST_UNKNOWN"
    requests.assert_called_once()


def test_reading_old_snapshot_does_not_approve_a_newer_proposal(m3):
    service, _, _ = m3
    create(service, "sop")
    upload(service)
    consumer = ArtifactClient(client(service, HUMAN), HUMAN.team, lambda: "synthetic-token")
    snapshot = consumer.get("report")
    upload(service, V2, b"a newer proposal")
    body = ApprovalInput(decision_id=V1, version_id=V1, decision="approve", sha256=consumer.version(snapshot, V1).sha256)
    with pytest.raises(Problem) as exc:
        consumer.decide("report", snapshot, body)
    assert exc.value.status == 412
    assert service.get(HUMAN, HUMAN.team, "report").payload["effective_version_id"] is None


def test_http_redirects_do_not_forward_credentials_to_another_origin():
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(307, headers={"Location": "https://outside.invalid/receive"}, json={
            "error": {"code": "REDIRECT_DENIED", "message": "synthetic redirect"},
        })

    with httpx.Client(base_url="https://testserver", transport=httpx.MockTransport(respond)) as http:
        consumer = ArtifactClient(http, HUMAN.team, lambda: "synthetic-token")
        with pytest.raises(Problem):
            consumer.get("report")
    assert len(seen) == 1 and seen[0].url.host == "testserver"


@pytest.mark.parametrize("payload", [
    b"not-json", b"[]", b'{"versions":[]}',
    b'{"error":{"code":"SYNTHETIC","message":"not an artifact"}}',
])
def test_invalid_metadata_is_never_presented_as_a_valid_artifact(payload):
    with httpx.Client(base_url="https://testserver", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, content=payload),
    )) as http:
        with pytest.raises(Problem):
            ArtifactClient(http, HUMAN.team, lambda: "synthetic-token").get("report")


