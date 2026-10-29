from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ServiceRequestError, ServiceResponseError
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.core.utils import CaseInsensitiveDict
from azure.storage.blob import ContainerClient

from conftest import A, B, TENANT
from shared_context.auth import Actor
from shared_context.backup_store import BlobBackupStore
from shared_context.errors import Problem

BACKUP_ID = "0123456789abcdef0123456789abcdef"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
VERSION = "2026-09-14T00:00:00.0000000Z"
ETAG = '"0x8NATIVE"'
ENDPOINT = "https://synthetic.blob.core.windows.net"
CONTAINER = "m1-environment-backups"
HEADERS = {
    "ETag": ETAG, "x-ms-version-id": VERSION, "Content-Length": "3",
    "Content-Type": "application/octet-stream", "x-ms-blob-type": "BlockBlob",
    "Last-Modified": "Mon, 14 Sep 2026 00:00:00 GMT",
    "x-ms-meta-tenant_id": TENANT, "x-ms-meta-owner_oid": A.oid,
    "x-ms-meta-backup_id": BACKUP_ID, "x-ms-meta-sha256": ABC_SHA256,
    "x-ms-meta-bytes": "3",
}


class BlobResponse(HttpResponse):
    def __init__(self, request, status, headers, data=b""):
        super().__init__(request, None)
        self.status_code = status
        self.headers = CaseInsensitiveDict(headers)
        self.content_type = headers.get("Content-Type", "application/octet-stream")
        self.reason = "synthetic response"
        self._data = data

    def body(self):
        return self._data

    def read(self):
        return self._data

    def stream_download(self, pipeline, **kwargs):
        return BlobStream(self._data)


class BlobStream:
    def __init__(self, data):
        self._chunks = iter([data])
        self.content_length = len(data)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._chunks)


class BlobTransport(HttpTransport):
    """Real SDK policies and serialization; all responses are local fixtures."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.closed = False

    def open(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected request, retry, or Azure management operation")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return BlobResponse(request, *response)


class Credential:
    def get_token(self, *scopes, **kwargs):
        assert scopes == ("https://storage.azure.com/.default",)
        return AccessToken("local-synthetic-token", 9999999999)


def blob_store(responses, tenant_id=TENANT):
    transport = BlobTransport(responses)
    container = ContainerClient(
        ENDPOINT, CONTAINER, credential=Credential(), transport=transport,
        retry_total=0, connection_timeout=10, read_timeout=20,
        max_single_put_size=8 * 1024 * 1024, max_single_get_size=8 * 1024 * 1024,
    )
    return BlobBackupStore(container, tenant_id), transport


def test_sdk_create_only_upload_returns_verified_native_blob_properties():
    store, transport = blob_store([
        (201, {"ETag": ETAG, "x-ms-version-id": VERSION}),
        (200, HEADERS),
    ])
    receipt = store.create(A, BACKUP_ID, b"abc", ABC_SHA256)
    assert receipt.model_dump() == {
        "backup_id": BACKUP_ID, "owner_oid": A.oid, "sha256": ABC_SHA256,
        "bytes": 3, "version_id": VERSION, "etag": ETAG,
    }
    put, head = transport.requests
    assert put.method == "PUT" and put.headers["If-None-Match"] == "*"
    assert put.body == b"abc"
    assert put.headers["x-ms-meta-owner_oid"] == A.oid
    assert put.headers["x-ms-meta-sha256"] == ABC_SHA256
    assert unquote(urlparse(put.url).path) == f"/{CONTAINER}/v1/{TENANT}/{A.oid}/{BACKUP_ID}"
    assert head.method == "HEAD"
    assert parse_qs(urlparse(head.url).query)["versionid"] == [VERSION]


def test_sdk_download_uses_the_exact_version_etag_and_bounded_range():
    store, transport = blob_store([
        (200, HEADERS),
        (206, {**HEADERS, "Content-Range": "bytes 0-2/3"}, b"abc"),
    ])
    downloaded = store.download(A, BACKUP_ID, VERSION, ETAG)
    assert downloaded.data == b"abc" and downloaded.receipt.sha256 == ABC_SHA256
    head, get = transport.requests
    assert head.method == "HEAD" and get.method == "GET"
    assert get.headers["If-Match"] == ETAG
    assert get.headers["x-ms-range"] == "bytes=0-2"
    for request in (head, get):
        assert parse_qs(urlparse(request.url).query)["versionid"] == [VERSION]


@pytest.mark.parametrize("headers,code", [
    ({**HEADERS, "Content-Range": "bytes 0-2/3"}, "BACKUP_INTEGRITY_ERROR"),
    (HEADERS, "BACKUP_PROTOCOL_ERROR"),
])
def test_corrupt_or_malformed_blob_download_is_an_explicit_error(headers, code):
    store, _ = blob_store([(200, HEADERS), (206, headers, b"abd")])
    with pytest.raises(Problem) as error:
        store.download(A, BACKUP_ID, VERSION, ETAG)
    assert error.value.status == 502
    assert error.value.body["error"]["code"] == code


def test_native_permission_error_retains_azure_status_and_code():
    store, transport = blob_store([
        (403, {"x-ms-error-code": "AuthorizationPermissionMismatch"}, b""),
    ])
    with pytest.raises(Problem) as error:
        store.receipt(A, BACKUP_ID)
    assert error.value.status == 502
    assert error.value.body["error"]["upstream_status"] == 403
    assert error.value.body["error"]["upstream_code"] == "AuthorizationPermissionMismatch"
    assert len(transport.requests) == 1


@pytest.mark.parametrize("status,code", [(409, "BlobAlreadyExists"), (412, "ConditionNotMet")])
def test_create_conflict_is_never_retried_or_replaced(status, code):
    store, transport = blob_store([(status, {"x-ms-error-code": code}, b"")])
    with pytest.raises(Problem) as error:
        store.create(A, BACKUP_ID, b"abc", ABC_SHA256)
    assert error.value.status == 409
    assert len(transport.requests) == 1
    assert transport.requests[0].headers["If-None-Match"] == "*"


@pytest.mark.parametrize("failure", [
    ServiceRequestError("synthetic connection failure"),
    ServiceResponseError("synthetic upload response lost"),
    (503, {"x-ms-error-code": "ServerBusy"}, b""),
])
def test_ambiguous_upload_failure_requires_explicit_reconciliation_not_a_mutation_retry(failure):
    store, transport = blob_store([failure, (200, HEADERS)])
    with pytest.raises(Problem) as error:
        store.create(A, BACKUP_ID, b"abc", ABC_SHA256)
    assert error.value.status == 502
    assert len(transport.requests) == 1
    assert store.receipt(A, BACKUP_ID).version_id == VERSION
    assert [request.method for request in transport.requests] == ["PUT", "HEAD"]


@pytest.mark.parametrize("headers", [
    {"ETag": ETAG}, {"x-ms-version-id": VERSION},
    {"ETag": ETAG, "x-ms-version-id": ""}, {"ETag": ETAG, "x-ms-version-id": "x" * 257},
])
def test_upload_without_valid_native_version_and_etag_never_succeeds(headers):
    store, transport = blob_store([(201, headers)])
    with pytest.raises(Problem) as error:
        store.create(A, BACKUP_ID, b"abc", ABC_SHA256)
    assert error.value.status == 502
    assert error.value.body["error"]["code"] == "BACKUP_PROTOCOL_ERROR"
    assert len(transport.requests) == 1


@pytest.mark.parametrize("changes,status", [
    ({"x-ms-version-id": ""}, 502), ({"ETag": ""}, 502),
    ({"x-ms-version-id": "different"}, 502),
    ({"x-ms-meta-owner_oid": B.oid}, 404), ({"x-ms-meta-tenant_id": B.oid}, 404),
    ({"x-ms-meta-backup_id": "f" * 32}, 502),
    ({"x-ms-meta-sha256": "not-a-hash"}, 502), ({"x-ms-meta-sha256": "A" * 64}, 502),
    ({"x-ms-meta-bytes": "03"}, 502), ({"x-ms-meta-bytes": "4"}, 502),
    ({"Content-Length": "8388609", "x-ms-meta-bytes": "8388609"}, 502),
    ({"Content-Length": "0", "x-ms-meta-bytes": "0"}, 502),
    ({"x-ms-blob-type": "PageBlob"}, 502), ({"Content-Type": "text/html"}, 502),
])
def test_untrusted_native_properties_cannot_produce_a_receipt_or_binary_response(changes, status):
    store, transport = blob_store([(200, {**HEADERS, **changes})])
    with pytest.raises(Problem) as error:
        store.download(A, BACKUP_ID, VERSION, ETAG)
    assert error.value.status == status
    assert len(transport.requests) == 1 and transport.requests[0].method == "HEAD"


@pytest.mark.parametrize("changes", [
    {"ETag": '"different"'}, {"x-ms-meta-sha256": "f" * 64},
])
def test_upload_acknowledgement_is_verified_against_stored_properties(changes):
    store, _ = blob_store([
        (201, {"ETag": ETAG, "x-ms-version-id": VERSION}),
        (200, {**HEADERS, **changes}),
    ])
    with pytest.raises(Problem) as error:
        store.create(A, BACKUP_ID, b"abc", ABC_SHA256)
    assert error.value.body["error"]["code"] == "BACKUP_INTEGRITY_ERROR"


def test_conditional_download_propagates_native_precondition_failure():
    store, transport = blob_store([
        (200, HEADERS), (412, {"x-ms-error-code": "ConditionNotMet"}, b""),
    ])
    with pytest.raises(Problem) as error:
        store.download(A, BACKUP_ID, VERSION, ETAG)
    assert error.value.status == 412
    assert len(transport.requests) == 2


def test_missing_native_backup_is_not_disclosed():
    store, _ = blob_store([(404, {"x-ms-error-code": "BlobNotFound"}, b"")])
    with pytest.raises(Problem) as error:
        store.receipt(B, BACKUP_ID, VERSION)
    assert error.value.status == 404
    assert error.value.body == {"error": {"code": "BACKUP_NOT_FOUND", "message": "backup not found"}}


def test_namespace_is_tenant_and_oid_bound_not_actor_or_team_label_bound():
    renamed = Actor(A.oid, "renamed-agent", "renamed-team")
    store, transport = blob_store([(200, HEADERS)])
    assert store.receipt(renamed, BACKUP_ID).owner_oid == A.oid
    assert f"/{TENANT}/{A.oid}/" in transport.requests[0].url
    other, other_transport = blob_store([
        (200, {**HEADERS, "x-ms-meta-tenant_id": B.oid}),
    ], tenant_id=B.oid)
    assert other.receipt(renamed, BACKUP_ID).owner_oid == A.oid
    assert f"/{B.oid}/{A.oid}/" in other_transport.requests[0].url


@pytest.mark.parametrize("backup_id,version", [
    ("../other", VERSION), ("F" * 32, VERSION), (BACKUP_ID, "line\nbreak"),
    (BACKUP_ID, ""), (BACKUP_ID, "x" * 257),
])
def test_invalid_store_lookup_cannot_send_a_request(backup_id, version):
    store, transport = blob_store([])
    with pytest.raises(Problem) as error:
        store.receipt(A, backup_id, version)
    assert error.value.status == 422 and transport.requests == []


def test_download_rejects_a_valid_hash_for_only_a_prefix_of_a_larger_native_blob():
    store, _ = blob_store([
        (200, HEADERS), (206, {**HEADERS, "Content-Range": "bytes 0-2/4"}, b"abc"),
    ])
    with pytest.raises(Problem) as error:
        store.download(A, BACKUP_ID, VERSION, ETAG)
    assert error.value.status == 502


def test_missing_configured_container_is_an_upstream_error_not_a_missing_backup():
    store, _ = blob_store([(404, {"x-ms-error-code": "ContainerNotFound"}, b"")])
    with pytest.raises(Problem) as error:
        store.receipt(A, BACKUP_ID)
    assert error.value.status == 502
    assert error.value.body["error"]["upstream_code"] == "ContainerNotFound"


def test_native_eight_mib_round_trip_stays_one_conditional_put_and_one_full_range_get():
    data = b"x" * 8388608
    sha256 = hashlib.sha256(data).hexdigest()
    headers = {**HEADERS, "Content-Length": "8388608", "x-ms-meta-bytes": "8388608",
               "x-ms-meta-sha256": sha256}
    store, transport = blob_store([
        (201, {"ETag": ETAG, "x-ms-version-id": VERSION}), (200, headers),
        (200, headers), (206, {**headers, "Content-Range": "bytes 0-8388607/8388608"}, data),
    ])
    receipt = store.create(A, BACKUP_ID, data, sha256)
    result = store.download(A, BACKUP_ID, receipt.version_id, receipt.etag)
    assert result.data == data and result.receipt.bytes == 8388608
    assert [request.method for request in transport.requests] == ["PUT", "HEAD", "HEAD", "GET"]
    assert transport.requests[0].headers["If-None-Match"] == "*"
    assert transport.requests[0].body == data
    assert transport.requests[-1].headers["x-ms-range"] == "bytes=0-8388607"


@pytest.mark.parametrize("etag", ["*", " * ", '"one", "two"', '"one",*', '"one"\n', '"one"\r\n'])
@pytest.mark.parametrize("operation", ["create", "receipt", "download"])
def test_unsafe_native_etags_cannot_produce_a_receipt_or_download(etag, operation):
    response = (
        (201, {"ETag": etag, "x-ms-version-id": VERSION})
        if operation == "create" else (200, {**HEADERS, "ETag": etag})
    )
    store, transport = blob_store([response])
    with pytest.raises(Problem) as error:
        if operation == "create":
            store.create(A, BACKUP_ID, b"abc", ABC_SHA256)
        elif operation == "receipt":
            store.receipt(A, BACKUP_ID, VERSION)
        else:
            store.download(A, BACKUP_ID, VERSION, ETAG)
    assert error.value.status == 502
    assert error.value.body["error"]["code"] == "BACKUP_PROTOCOL_ERROR"
    assert len(transport.requests) == 1
