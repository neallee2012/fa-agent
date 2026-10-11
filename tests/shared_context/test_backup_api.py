from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import A, B, AUDIENCE, TENANT
from shared_context.api import create_app
from shared_context.auth import JwtVerifier
from shared_context.backup_models import BackupReceipt
from shared_context.backup_store import InMemoryBackupStore

BACKUP_ID = "0123456789abcdef0123456789abcdef"
BACKUP_PATH = f"/private-environments/backups/{BACKUP_ID}"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def backup_system(system, auth, signing_key):
    keys = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=signing_key.public_key()),
    )
    store = InMemoryBackupStore(TENANT)
    app = create_app(system[0], JwtVerifier(auth, keys), None, store)

    def headers(actor=A, **claims):
        token = jwt.encode({
            "exp": int(time.time()) + 300, "iss": auth.issuers[0],
            "aud": AUDIENCE, "tid": TENANT, "oid": actor.oid, **claims,
        }, signing_key, algorithm="RS256", headers={"kid": "local-test"})
        return {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/octet-stream",
            "X-Backup-SHA256": ABC_SHA256,
        }

    return TestClient(app), headers, store


def test_upload_returns_an_exact_owner_bound_version_receipt(backup_system):
    client, headers, _ = backup_system
    response = client.put(BACKUP_PATH, content=b"abc", headers=headers())
    assert response.status_code == 201
    receipt = BackupReceipt.model_validate(response.json())
    assert receipt.model_dump() == {
        "backup_id": BACKUP_ID, "owner_oid": A.oid, "sha256": ABC_SHA256,
        "bytes": 3, "version_id": receipt.version_id, "etag": receipt.etag,
    }
    assert receipt.version_id and receipt.etag
    assert response.headers["cache-control"] == "no-store"


def test_duplicate_upload_can_be_reconciled_without_overwriting(backup_system):
    client, headers, _ = backup_system
    receipt = client.put(BACKUP_PATH, content=b"abc", headers=headers()).json()
    replacement = {**headers(), "X-Backup-SHA256": hashlib.sha256(b"replacement").hexdigest()}
    assert client.put(BACKUP_PATH, content=b"replacement", headers=replacement).status_code == 409
    current = client.get(BACKUP_PATH + "/receipt", headers=headers())
    pinned = client.get(
        BACKUP_PATH + "/receipt", params={"version_id": receipt["version_id"]}, headers=headers(),
    )
    assert current.status_code == pinned.status_code == 200
    assert current.json() == pinned.json() == receipt


def test_download_is_pinned_to_the_receipt_version_and_etag(backup_system):
    client, headers, _ = backup_system
    receipt = client.put(BACKUP_PATH, content=b"abc", headers=headers()).json()
    response = client.get(
        BACKUP_PATH, params={"version_id": receipt["version_id"]},
        headers={**headers(), "If-Match": receipt["etag"]},
    )
    assert response.status_code == 200 and response.content == b"abc"
    assert response.headers["content-length"] == "3"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-backup-sha256"] == ABC_SHA256
    assert response.headers["x-backup-version-id"] == receipt["version_id"]
    assert response.headers["etag"] == receipt["etag"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("claims,status", [
    (None, 401), ({"exp": 1}, 401), ({"aud": "https://ai.azure.com"}, 401),
    ({"tid": B.oid}, 401), ({"oid": "dddddddd-dddd-dddd-dddd-dddddddddddd"}, 403),
])
def test_authentication_rejects_before_receiving_any_binary_body(backup_system, claims, status):
    client, headers, _ = backup_system
    supplied = headers(**claims) if claims is not None else {}
    supplied["Content-Length"] = str(8 * 1024 * 1024 + 1)

    async def no_receive():
        pytest.fail("unauthorized backup must not consume the binary body")

    messages = asyncio.run(asgi_put(client.app, supplied, no_receive))
    assert messages[0]["status"] == status


async def asgi_put(app, headers, receive):
    messages = []

    async def send(message):
        messages.append(message)

    await app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "PUT", "scheme": "https", "path": BACKUP_PATH, "root_path": "",
        "query_string": b"", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "server": ("testserver", 443), "client": ("127.0.0.1", 10000),
    }, receive, send)
    return messages


def test_exact_eight_mib_round_trip_does_not_raise_existing_json_limit(backup_system):
    client, headers, _ = backup_system
    data = b"x" * (8 * 1024 * 1024)
    supplied = {**headers(), "X-Backup-SHA256": hashlib.sha256(data).hexdigest()}
    response = client.put(BACKUP_PATH, content=data, headers=supplied)
    assert response.status_code == 201
    receipt = response.json()
    downloaded = client.get(
        BACKUP_PATH, params={"version_id": receipt["version_id"]},
        headers={**headers(), "If-Match": receipt["etag"]},
    )
    assert downloaded.content == data
    assert downloaded.headers["content-length"] == str(8 * 1024 * 1024)
    context = client.post(
        "/teams/team-one/works/oversized", content=b"x" * 65537,
        headers={**headers(), "Content-Type": "application/json"},
    )
    assert context.status_code == 413


@pytest.mark.parametrize("declared", [None, "3", str(8 * 1024 * 1024 + 1)])
def test_oversized_streams_never_create_a_backup(backup_system, declared):
    client, headers, _ = backup_system
    supplied = headers()
    if declared is not None:
        supplied["Content-Length"] = declared
    chunks = iter([
        {"type": "http.request", "body": b"x" * (8 * 1024 * 1024), "more_body": True},
        {"type": "http.request", "body": b"x", "more_body": False},
    ])

    async def receive():
        return next(chunks)

    messages = asyncio.run(asgi_put(client.app, supplied, receive))
    assert messages[0]["status"] == 413
    assert client.get(BACKUP_PATH + "/receipt", headers=headers()).status_code == 404


@pytest.mark.parametrize("body,overrides,status", [
    (b"", {}, 422), (b"abd", {}, 422), (b"abc", {"X-Backup-SHA256": "A" * 64}, 422),
    (b"abc", {"X-Backup-SHA256": "not-a-hash"}, 422),
    (b"abc", {"Content-Type": "application/json"}, 415),
    (b"abc", {"Content-Encoding": "gzip"}, 415),
])
def test_invalid_uploads_leave_no_receipt(backup_system, body, overrides, status):
    client, headers, _ = backup_system
    response = client.put(BACKUP_PATH, content=body, headers={**headers(), **overrides})
    assert response.status_code == status
    assert client.get(BACKUP_PATH + "/receipt", headers=headers()).status_code == 404


@pytest.mark.parametrize("version,etag,status", [
    (None, "receipt", 422), ("", "receipt", 422), ("missing", "receipt", 404),
    ("receipt", None, 428), ("receipt", "*", 412), ("receipt", '"other"', 412),
    ("receipt", '"one","two"', 412), ("receipt", "x" * 257, 412),
    ("receipt", " * ", 412), ("receipt", '"one",*', 412),
    ("receipt", '"one"\n', 412), ("receipt", '"one"\r\n', 412),
])
def test_download_rejects_missing_or_mismatched_version_and_etag(backup_system, version, etag, status):
    client, headers, _ = backup_system
    receipt = client.put(BACKUP_PATH, content=b"abc", headers=headers()).json()
    params = {} if version is None else {"version_id": receipt["version_id"] if version == "receipt" else version}
    supplied = headers()
    if etag is not None:
        supplied["If-Match"] = receipt["etag"] if etag == "receipt" else etag
    assert client.get(BACKUP_PATH, params=params, headers=supplied).status_code == status


def test_download_rejects_multiple_if_match_headers_even_when_each_matches(backup_system):
    client, headers, _ = backup_system
    receipt = client.put(BACKUP_PATH, content=b"abc", headers=headers()).json()
    supplied = [*headers().items(), ("If-Match", receipt["etag"]), ("If-Match", receipt["etag"])]
    response = client.get(BACKUP_PATH, params={"version_id": receipt["version_id"]}, headers=supplied)
    assert response.status_code == 412


def test_other_owner_and_missing_id_are_indistinguishable(backup_system):
    client, headers, _ = backup_system
    receipt = client.put(BACKUP_PATH, content=b"abc", headers={
        **headers(actor=A, team=B.team), "X-Owner-Oid": B.oid, "X-Actor": B.actor,
    }).json()
    assert receipt["owner_oid"] == A.oid
    for suffix, params in (
        ("/receipt", {}), ("/receipt", {"version_id": receipt["version_id"]}),
        ("", {"version_id": receipt["version_id"]}),
    ):
        supplied = {**headers(actor=B), "If-Match": receipt["etag"]}
        other = client.get(BACKUP_PATH + suffix, params=params, headers=supplied)
        missing = client.get(
            "/private-environments/backups/" + "f" * 32 + suffix, params=params, headers=supplied,
        )
        assert other.status_code == missing.status_code == 404
        assert other.json() == missing.json()
    assert client.put(BACKUP_PATH, content=b"abc", headers=headers(actor=B)).status_code == 201


@pytest.mark.parametrize("query", [
    {"owner_oid": B.oid}, {"url": "https://example.invalid/archive"},
    {"path": "../another-owner"}, [("version_id", "one"), ("version_id", "two")],
])
def test_query_parameters_cannot_supply_authority_or_arbitrary_locations(backup_system, query):
    client, headers, _ = backup_system
    assert client.put(BACKUP_PATH, content=b"abc", params=query, headers=headers()).status_code == 422
    assert client.get(BACKUP_PATH + "/receipt", params=query, headers=headers()).status_code == 422


def test_no_delete_list_all_or_feature_without_opt_in(backup_system, system):
    client, headers, _ = backup_system
    assert client.delete(BACKUP_PATH, headers=headers()).status_code == 405
    assert client.get("/private-environments/backups", headers=headers()).status_code == 404
    disabled = TestClient(create_app(system[0], lambda: A))
    assert disabled.put(BACKUP_PATH, content=b"abc", headers=headers()).status_code == 404
    assert disabled.put(BACKUP_PATH, content=b"x" * 65537, headers=headers()).status_code == 413


def test_concurrent_creates_have_one_winner_and_a_reconcilable_receipt(backup_system):
    client, headers, _ = backup_system
    supplied = headers()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.put(BACKUP_PATH, content=b"abc", headers=supplied), range(2)))
    assert sorted(result.status_code for result in results) == [201, 409]
    saved = next(result.json() for result in results if result.status_code == 201)
    assert client.get(BACKUP_PATH + "/receipt", headers=supplied).json() == saved


def test_backup_configuration_is_opt_in_and_requires_a_fixed_resource():
    from shared_context.config import BackupSettings

    assert BackupSettings.from_env({}) is None
    settings = BackupSettings.from_env({
        "CONTEXT_BACKUP_BLOB_ENDPOINT": "https://stfam1backupdemo.blob.core.windows.net/",
        "CONTEXT_BACKUP_CONTAINER": "m1-environment-backups",
    })
    assert settings.endpoint == "https://stfam1backupdemo.blob.core.windows.net"
    assert settings.container == "m1-environment-backups"
    for values in (
        {"CONTEXT_BACKUP_BLOB_ENDPOINT": "https://stfam1backupdemo.blob.core.windows.net"},
        {"CONTEXT_BACKUP_CONTAINER": "m1-environment-backups"},
        {"CONTEXT_BACKUP_BLOB_ENDPOINT": "", "CONTEXT_BACKUP_CONTAINER": ""},
    ):
        with pytest.raises(ValueError, match="CONTEXT_BACKUP"):
            BackupSettings.from_env(values)


@pytest.mark.parametrize("field,value", [
    ("backup_id", "A" * 32), ("backup_id", "a" * 31),
    ("owner_oid", A.oid.upper()), ("owner_oid", "not-a-uuid"),
    ("sha256", "A" * 64), ("sha256", "a" * 63),
    ("bytes", 0), ("bytes", 8388609), ("bytes", True), ("bytes", "3"),
    ("version_id", ""), ("version_id", "x" * 257), ("version_id", "bad\n"),
    ("version_id", "\u00e9"), ("version_id", "\x7f"),
    ("etag", ""), ("etag", "x" * 257), ("etag", "bad\r\n"), ("etag", "\x80"),
    ("unexpected_owner", B.oid),
])
def test_receipt_model_rejects_unsafe_or_noncanonical_receipts(field, value):
    receipt = {
        "backup_id": BACKUP_ID, "owner_oid": A.oid, "sha256": ABC_SHA256,
        "bytes": 3, "version_id": "native-version", "etag": '"native-etag"',
    }
    with pytest.raises(ValidationError):
        BackupReceipt.model_validate({**receipt, field: value})


@pytest.mark.parametrize("etag", ["*", " * ", '"one", "two"', '"one",*', '"one"\n', '"one"\r\n'])
def test_receipt_requires_a_single_non_wildcard_etag(etag):
    with pytest.raises(ValidationError):
        BackupReceipt(
            backup_id=BACKUP_ID, owner_oid=A.oid, sha256=ABC_SHA256,
            bytes=3, version_id="native-version", etag=etag,
        )


@pytest.mark.parametrize("endpoint", [
    "http://synthetic.blob.core.windows.net", "https://evil.example",
    "https://synthetic.blob.core.windows.net.evil.example",
    "https://synthetic.blob.core.windows.net/container",
    "https://synthetic.blob.core.windows.net?sig=untrusted",
    "https://synthetic.blob.core.windows.net#fragment",
    "https://user@synthetic.blob.core.windows.net",
    "https://synthetic.blob.core.windows.net:8443",
    "https://sub.synthetic.blob.core.windows.net",
    "https://synthetic.privatelink.blob.core.windows.net",
    "https://synthetic.blob.core.windows.net//",
])
def test_backup_configuration_rejects_noncanonical_or_untrusted_destinations(endpoint):
    from shared_context.config import BackupSettings

    with pytest.raises(ValueError, match="CONTEXT_BACKUP_BLOB_ENDPOINT"):
        BackupSettings.from_env({
            "CONTEXT_BACKUP_BLOB_ENDPOINT": endpoint,
            "CONTEXT_BACKUP_CONTAINER": "m1-environment-backups",
        })


@pytest.mark.parametrize("container", ["ab", "a" * 64, "../other", "UpperCase", "bad--name", "-bad", "bad-"])
def test_backup_configuration_rejects_invalid_containers(container):
    from shared_context.config import BackupSettings

    with pytest.raises(ValueError, match="CONTEXT_BACKUP_CONTAINER"):
        BackupSettings("https://synthetic.blob.core.windows.net", container)


def test_shared_receipt_and_store_imports_do_not_require_the_blob_sdk():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    result = subprocess.run([sys.executable, "-c", """
import importlib.abc
import sys
class NoBlobSDK(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'azure.storage.blob':
            raise AssertionError('shared imports must not require the Blob SDK')
sys.meta_path.insert(0, NoBlobSDK())
from shared_context.backup_models import BackupReceipt
from shared_context.backup_store import InMemoryBackupStore
assert 'azure.storage.blob' not in sys.modules
"""], env={**os.environ, "PYTHONPATH": str(source)}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
