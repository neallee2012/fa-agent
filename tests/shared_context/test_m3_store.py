from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from azure.storage.blob import ContainerClient
from azure.core.exceptions import ServiceRequestError, ServiceResponseError

from conftest import TENANT
from test_backup_store import BlobTransport, Credential
from shared_context.errors import Problem
from shared_context.m3_models import BlobReceipt, MAX_ARTIFACT_BYTES
from shared_context.m3_store import ArtifactStore, InMemoryArtifactStore

TEAM = "team-one"
ARTIFACT = "release-notes"
LOGICAL_VERSION = "0123456789abcdef0123456789abcdef"
ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
NATIVE_VERSION = "2026-09-14T14:00:00.0000000Z"
ETAG = '"0x8M3NATIVE"'
CONTAINER = "artifact-versions"
HEADERS = {
    "ETag": ETAG, "x-ms-version-id": NATIVE_VERSION, "Content-Length": "3",
    "Content-Type": "application/octet-stream", "x-ms-blob-type": "BlockBlob",
    "Last-Modified": "Mon, 14 Sep 2026 14:00:00 GMT",
    "x-ms-meta-tenant_id": TENANT, "x-ms-meta-team": TEAM, "x-ms-meta-artifact_id": ARTIFACT,
    "x-ms-meta-logical_version_id": LOGICAL_VERSION, "x-ms-meta-sha256": ABC_SHA256,
    "x-ms-meta-bytes": "3",
}


@pytest.fixture
def blobs():
    with ExitStack() as clients:
        def make(responses, tenant_id=TENANT, single_put_size=512 * 1024):
            from shared_context.m3_store import BlobArtifactStore

            transport = BlobTransport(responses)
            container = clients.enter_context(ContainerClient(
                "https://synthetic.blob.core.windows.net", CONTAINER, credential=Credential(),
                transport=transport, retry_total=3, retry_backoff_factor=0,
                max_single_put_size=single_put_size, max_single_get_size=512 * 1024,
            ))
            store = BlobArtifactStore(container, tenant_id)
            assert transport.requests == []
            return store, transport
        yield make


def test_memory_store_preserves_an_immutable_version_and_exact_receipt():
    store: ArtifactStore = InMemoryArtifactStore(TENANT)
    receipt = store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert isinstance(receipt, BlobReceipt)
    assert set(receipt.model_dump()) == {"native_version", "etag", "sha256", "bytes"}
    assert receipt.sha256 == ABC_SHA256 and receipt.bytes == 3
    assert receipt.native_version and receipt.etag
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION) == receipt
    assert store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt) == b"abc"
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert error.value.status == 409
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION) == receipt


def test_real_sdk_put_is_create_only_and_confirms_the_native_receipt(blobs):
    store, transport = blobs([
        (201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}), (200, HEADERS), (200, HEADERS),
    ])
    receipt = store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert receipt.model_dump() == {
        "native_version": NATIVE_VERSION, "etag": ETAG, "sha256": ABC_SHA256, "bytes": 3,
    }
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION) == receipt
    put, confirmed, current = transport.requests
    assert put.method == "PUT" and put.body == b"abc"
    assert put.headers["If-None-Match"] == "*"
    assert put.headers["x-ms-blob-type"] == "BlockBlob"
    assert put.headers["x-ms-blob-content-type"] == "application/octet-stream"
    for name in ("tenant_id", "team", "artifact_id", "logical_version_id", "sha256", "bytes"):
        assert put.headers["x-ms-meta-" + name] == HEADERS["x-ms-meta-" + name]
    for request in transport.requests:
        assert unquote(urlparse(request.url).path) == (
            f"/{CONTAINER}/m3/v1/{TENANT}/{TEAM}/{ARTIFACT}/{LOGICAL_VERSION}"
        )
        assert parse_qs(urlparse(request.url).query)["timeout"] == ["20"]
    assert confirmed.method == current.method == "HEAD"
    assert parse_qs(urlparse(confirmed.url).query)["versionid"] == [NATIVE_VERSION]
    assert "versionid" not in parse_qs(urlparse(current.url).query)


def test_real_sdk_read_pins_native_version_exact_etag_and_full_bounded_range(blobs):
    store, transport = blobs([
        (200, HEADERS), (206, {**HEADERS, "Content-Range": "bytes 0-2/3"}, b"abc"),
    ])
    receipt = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    assert store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt) == b"abc"
    assert [request.method for request in transport.requests] == ["HEAD", "GET"]
    get = transport.requests[-1]
    assert get.headers["If-Match"] == ETAG and get.headers["x-ms-range"] == "bytes=0-2"
    for request in transport.requests:
        assert parse_qs(urlparse(request.url).query)["versionid"] == [NATIVE_VERSION]
        assert unquote(urlparse(request.url).path) == (
            f"/{CONTAINER}/m3/v1/{TENANT}/{TEAM}/{ARTIFACT}/{LOGICAL_VERSION}"
        )


@pytest.mark.parametrize("adapter", ["memory", "blob"])
def test_exact_512_kib_round_trip_is_supported_and_one_extra_byte_never_writes(blobs, adapter):
    data = b"x" * (512 * 1024)
    digest = hashlib.sha256(data).hexdigest()
    assert len(data) == MAX_ARTIFACT_BYTES
    headers = {**HEADERS, "Content-Length": str(len(data)), "x-ms-meta-bytes": str(len(data)),
               "x-ms-meta-sha256": digest}
    store, transport = blobs([
        (201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}), (200, headers),
        (200, headers), (206, {**headers, "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"}, data),
    ])
    if adapter == "memory":
        store = InMemoryArtifactStore(TENANT)
    receipt = store.put(TEAM, ARTIFACT, LOGICAL_VERSION, data, digest)
    assert store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt) == data
    before = len(transport.requests)
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, "f" * 32, data + b"x", hashlib.sha256(data + b"x").hexdigest())
    assert error.value.status == 413 and len(transport.requests) == before
    if adapter == "blob":
        assert [request.method for request in transport.requests] == ["PUT", "HEAD", "HEAD", "GET"]


@pytest.mark.parametrize("adapter", ["memory", "blob"])
@pytest.mark.parametrize("data,digest", [
    (b"", ABC_SHA256), ("abc", ABC_SHA256), (bytearray(b"abc"), ABC_SHA256),
    (b"abd", ABC_SHA256), (b"abc", ABC_SHA256.upper()), (b"abc", "not-a-hash"),
])
def test_invalid_payloads_fail_before_any_blob_request(blobs, adapter, data, digest):
    store, transport = blobs([])
    if adapter == "memory":
        store = InMemoryArtifactStore(TENANT)
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, data, digest)
    assert error.value.status == 422 and transport.requests == []


@pytest.mark.parametrize("adapter", ["memory", "blob"])
@pytest.mark.parametrize("key", [
    ("../team", ARTIFACT, LOGICAL_VERSION), (TEAM, "https://outside.invalid/file", LOGICAL_VERSION),
    (TEAM, ARTIFACT, "../version"), (TEAM, ARTIFACT, LOGICAL_VERSION.upper()),
    ("team\n", ARTIFACT, LOGICAL_VERSION), (TEAM, "a" * 65, LOGICAL_VERSION),
])
def test_lookup_identifiers_cannot_escape_the_fixed_namespace(blobs, adapter, key):
    store, transport = blobs([])
    if adapter == "memory":
        store = InMemoryArtifactStore(TENANT)
    receipt = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    for operation in (
        lambda: store.put(*key, b"abc", ABC_SHA256),
        lambda: store.stat(*key),
        lambda: store.read(*key, receipt),
    ):
        with pytest.raises(Problem) as error:
            operation()
        assert error.value.status == 422
    assert transport.requests == []


@pytest.mark.parametrize("tenant", ["not-a-uuid", "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", None, 123])
def test_both_adapters_require_a_canonical_tenant_uuid(blobs, tenant):
    with pytest.raises(ValueError, match="tenant_id"):
        InMemoryArtifactStore(tenant)
    with pytest.raises(ValueError, match="tenant_id"):
        blobs([], tenant_id=tenant)


def test_memory_receipts_are_copies_and_concurrent_creates_have_one_winner():
    store = InMemoryArtifactStore(TENANT)

    def upload(_):
        try:
            return store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
        except Problem as exc:
            assert exc.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload, range(2)))
    assert sum(result is not None for result in results) == 1
    accepted = store.stat(TEAM, ARTIFACT, LOGICAL_VERSION)
    returned = next(result for result in results if result is not None)
    returned.sha256 = "f" * 64
    changed_stat = store.stat(TEAM, ARTIFACT, LOGICAL_VERSION)
    changed_stat.etag = '"changed"'
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION) == accepted
    assert store.read(TEAM, ARTIFACT, LOGICAL_VERSION, accepted) == b"abc"


@pytest.mark.parametrize("key", [
    ("team-two", ARTIFACT, LOGICAL_VERSION), (TEAM, "other-artifact", LOGICAL_VERSION),
    (TEAM, ARTIFACT, "f" * 32),
])
def test_memory_receipts_cannot_be_reused_for_other_keys(key):
    store = InMemoryArtifactStore(TENANT)
    receipt = store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    with pytest.raises(Problem) as error:
        store.stat(*key)
    assert error.value.status == 404
    store.put(*key, b"abc", ABC_SHA256)
    with pytest.raises(Problem) as error:
        store.read(*key, receipt)
    assert error.value.status == 404


@pytest.mark.parametrize("adapter", ["memory", "blob"])
@pytest.mark.parametrize("changes,status", [
    ({"etag": '"different"'}, 412), ({"sha256": "f" * 64}, 502), ({"bytes": 4}, 502),
    ({"etag": "*"}, 422), ({"etag": '"first","second"'}, 422), ({"etag": "bad\netag"}, 422),
    ({"native_version": ""}, 422), ({"native_version": "x" * 257}, 422),
    ({"bytes": 524289}, 422), ({"bytes": True}, 422),
])
def test_supplied_receipts_are_revalidated_and_must_match_exactly(blobs, adapter, changes, status):
    store, transport = blobs([(200, HEADERS)])
    receipt = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    if adapter == "memory":
        store = InMemoryArtifactStore(TENANT)
        receipt = store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    with pytest.raises(Problem) as error:
        store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt.model_copy(update=changes))
    assert error.value.status == status
    assert len(transport.requests) == (1 if adapter == "blob" and status != 422 else 0)


@pytest.mark.parametrize("changes,status", [
    ({"x-ms-meta-tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}, 404),
    ({"x-ms-meta-team": "team-two"}, 404),
    ({"x-ms-meta-artifact_id": "other-artifact"}, 502),
    ({"x-ms-meta-logical_version_id": "f" * 32}, 502),
    ({"x-ms-meta-sha256": "not-a-hash"}, 502), ({"x-ms-meta-sha256": ABC_SHA256.upper()}, 502),
    ({"x-ms-meta-bytes": "03"}, 502), ({"x-ms-meta-bytes": "4"}, 502),
    ({"Content-Length": "524289", "x-ms-meta-bytes": "524289"}, 502),
    ({"Content-Length": "0", "x-ms-meta-bytes": "0"}, 502),
    ({"Content-Type": "text/plain"}, 502), ({"x-ms-blob-type": "PageBlob"}, 502),
    ({"x-ms-version-id": ""}, 502), ({"x-ms-version-id": "x" * 257}, 502),
    ({"ETag": ""}, 502), ({"ETag": "*"}, 502), ({"ETag": '"one","two"'}, 502),
])
def test_stat_rejects_cross_namespace_or_inconsistent_native_metadata(blobs, changes, status):
    store, transport = blobs([(200, {**HEADERS, **changes})])
    with pytest.raises(Problem) as error:
        store.stat(TEAM, ARTIFACT, LOGICAL_VERSION)
    assert error.value.status == status
    assert len(transport.requests) == 1


@pytest.mark.parametrize("changes,status", [
    ({"x-ms-meta-tenant_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}, 404),
    ({"x-ms-meta-team": "team-two"}, 404), ({"x-ms-meta-artifact_id": "other-artifact"}, 502),
    ({"x-ms-meta-logical_version_id": "f" * 32}, 502), ({"x-ms-meta-sha256": "f" * 64}, 502),
    ({"x-ms-meta-bytes": "4"}, 502), ({"x-ms-version-id": "other-native-version"}, 502),
    ({"ETag": '"different"'}, 412), ({"Content-Type": "text/html"}, 502),
])
def test_download_rechecks_metadata_after_the_pinned_head(blobs, changes, status):
    store, transport = blobs([
        (200, HEADERS), (206, {**HEADERS, "Content-Range": "bytes 0-2/3", **changes}, b"abc"),
    ])
    receipt = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    with pytest.raises(Problem) as error:
        store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt)
    assert error.value.status == status and len(transport.requests) == 2


@pytest.mark.parametrize("body,content_range", [
    (b"abd", "bytes 0-2/3"), (b"ab", "bytes 0-2/3"), (b"abcd", "bytes 0-2/3"),
    (b"abc", "bytes 0-2/6"), (b"abc", "bytes 0-2/524289"), (b"abc", None),
])
def test_actual_hash_length_and_entire_range_are_required(blobs, body, content_range):
    headers = dict(HEADERS)
    if content_range is not None:
        headers["Content-Range"] = content_range
    store, transport = blobs([(200, HEADERS), (206, headers, body)])
    receipt = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    with pytest.raises(Problem) as error:
        store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt)
    assert error.value.status == 502 and len(transport.requests) == 2


@pytest.mark.parametrize("phase", ["stat", "read-head", "read-get"])
@pytest.mark.parametrize("status,code,expected", [
    (404, "BlobNotFound", 404), (404, "ContainerNotFound", 502),
    (403, "AuthorizationPermissionMismatch", 502), (401, "AuthenticationFailed", 502),
    (503, "ServerBusy", 502), (412, "ConditionNotMet", 412),
])
def test_native_errors_distinguish_missing_objects_from_configuration_and_acl_failures(blobs, phase, status, code, expected):
    responses = [(200, HEADERS)] if phase == "read-get" else []
    responses.append((status, {"x-ms-error-code": code}, b""))
    store, transport = blobs(responses)
    receipt = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    with pytest.raises(Problem) as error:
        if phase == "stat":
            store.stat(TEAM, ARTIFACT, LOGICAL_VERSION)
        else:
            store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt)
    expected = 502 if status == 412 and phase != "read-get" else expected
    assert error.value.status == expected
    if expected == 502:
        assert error.value.body["error"]["upstream_status"] == status
        assert error.value.body["error"]["upstream_code"] == code
    assert len(transport.requests) == (2 if phase == "read-get" else 1)


@pytest.mark.parametrize("failure", [
    ServiceRequestError("synthetic connection failure"), ServiceResponseError("synthetic lost upload response"),
    (503, {"x-ms-error-code": "ServerBusy"}, b""),
])
def test_unknown_upload_is_not_retried_and_stat_is_explicit_reconciliation(blobs, failure):
    store, transport = blobs([failure, (200, HEADERS)])
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert error.value.status == 502 and len(transport.requests) == 1
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION).native_version == NATIVE_VERSION
    assert [request.method for request in transport.requests] == ["PUT", "HEAD"]


@pytest.mark.parametrize("status,code", [(409, "BlobAlreadyExists"), (412, "ConditionNotMet")])
@pytest.mark.parametrize("data", [b"abc", b"xyz"])
def test_existing_logical_versions_are_never_overwritten_even_for_identical_bytes(blobs, status, code, data):
    store, transport = blobs([
        (201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}), (200, HEADERS),
        (status, {"x-ms-error-code": code}, b""), (200, HEADERS),
    ])
    original = store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, data, hashlib.sha256(data).hexdigest())
    assert error.value.status == 409
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION) == original
    assert [request.method for request in transport.requests] == ["PUT", "HEAD", "PUT", "HEAD"]
    assert all(request.headers["If-None-Match"] == "*" for request in transport.requests if request.method == "PUT")


@pytest.mark.parametrize("headers", [
    {"ETag": ETAG}, {"x-ms-version-id": NATIVE_VERSION},
    {"ETag": ETAG, "x-ms-version-id": ""}, {"ETag": "*", "x-ms-version-id": NATIVE_VERSION},
])
def test_upload_without_a_native_version_and_valid_etag_has_no_success_fallback(blobs, headers):
    store, transport = blobs([(201, headers)])
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert error.value.status == 502 and len(transport.requests) == 1


@pytest.mark.parametrize("changes", [{"ETag": '"different"'}, {"x-ms-meta-sha256": "f" * 64}])
def test_put_acknowledgement_must_match_the_native_version_readback(blobs, changes):
    store, transport = blobs([
        (201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}), (200, {**HEADERS, **changes}),
    ])
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert error.value.status == 502
    assert [request.method for request in transport.requests] == ["PUT", "HEAD"]


def test_failed_readback_never_attempts_a_repair_write(blobs):
    store, transport = blobs([
        (201, {"ETag": ETAG, "x-ms-version-id": NATIVE_VERSION}),
        ServiceResponseError("synthetic readback failure"), (200, HEADERS),
    ])
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert error.value.status == 502
    assert [request.method for request in transport.requests] == ["PUT", "HEAD"]
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION).native_version == NATIVE_VERSION


def test_read_never_substitutes_the_current_blob_for_an_accepted_native_version(blobs):
    store, transport = blobs([
        (200, {**HEADERS, "x-ms-version-id": "new-native-version", "ETag": '"new-etag"'}),
        (200, HEADERS), (206, {**HEADERS, "Content-Range": "bytes 0-2/3"}, b"abc"),
    ])
    assert store.stat(TEAM, ARTIFACT, LOGICAL_VERSION).native_version == "new-native-version"
    accepted = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    assert store.read(TEAM, ARTIFACT, LOGICAL_VERSION, accepted) == b"abc"
    for request in transport.requests[1:]:
        assert parse_qs(urlparse(request.url).query)["versionid"] == [NATIVE_VERSION]


@pytest.mark.parametrize("failure", [
    ServiceRequestError("synthetic GET connection failure"), ServiceResponseError("synthetic interrupted GET"),
])
def test_sdk_download_recovery_cannot_issue_another_network_request(blobs, failure):
    store, transport = blobs([(200, HEADERS), failure])
    receipt = BlobReceipt(native_version=NATIVE_VERSION, etag=ETAG, sha256=ABC_SHA256, bytes=3)
    with pytest.raises(Problem) as error:
        store.read(TEAM, ARTIFACT, LOGICAL_VERSION, receipt)
    assert error.value.status == 502
    assert [request.method for request in transport.requests] == ["HEAD", "GET"]


def test_sdk_configuration_cannot_replace_create_only_put_with_staged_block_writes(blobs):
    store, transport = blobs([], single_put_size=1)
    with pytest.raises(Problem) as error:
        store.put(TEAM, ARTIFACT, LOGICAL_VERSION, b"abc", ABC_SHA256)
    assert error.value.status == 502
    assert transport.requests == []
