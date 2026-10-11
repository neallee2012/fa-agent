"""Expiry at the real Blob SDK boundary using local transport, not Azure proof."""

from contextlib import contextmanager
from datetime import timedelta

import pytest
from azure.storage.blob import ContainerClient

from shared_context.errors import Problem
from shared_context.m3_store import BlobArtifactStore
from shared_context.source_materials import InventoryProtectionVerifier, SourceMaterials
from test_backup_store import BlobTransport, Credential
from test_source_expiry import expiring_service
from test_source_materials import (
    CONFIG, CONTEXT, ETAG, NATIVE, TENANT, blob_headers, blob_reads, code, input_source, native_state,
)


@contextmanager
def large_sources():
    simple, table, clock = expiring_service()

    class ExpiringTransport(BlobTransport):
        expire_on_get = False

        def send(self, request, **kwargs):
            result = super().send(request, **kwargs)
            if self.expire_on_get and request.method == "GET":
                clock.value += timedelta(minutes=1)
            return result

    transport = ExpiringTransport([])
    with ContainerClient("https://synthetic.blob.core.windows.net", "originals", credential=Credential(),
                         transport=transport, max_single_put_size=512 * 1024, max_single_get_size=512 * 1024) as container:
        sources = SourceMaterials(
            simple.store, clock=clock, blob_store=BlobArtifactStore(container, TENANT), protection=CONFIG,
            protection_verifier=InventoryProtectionVerifier(lambda: native_state(), clock=clock),
        )
        sources.initialize_protection()
        yield sources, table, clock, transport


def test_expired_large_preparation_never_sends_a_blob_PUT():
    with large_sources() as (sources, _, clock, transport):
        expiry = clock.value + timedelta(seconds=1)
        plan = sources.plan(CONTEXT, input_source(expires_at=expiry), b"x" * 33000)
        clock.value = expiry
        with pytest.raises(Problem) as expired:
            sources.prepare_large(CONTEXT, plan)
        assert code(expired) == "SOURCE_EXPIRED"
        assert not transport.requests


def test_expiry_during_native_download_never_returns_original_bytes():
    with large_sources() as (sources, _, clock, transport):
        plan = sources.plan(CONTEXT, input_source(expires_at=clock.value + timedelta(seconds=1)), b"x" * 33000)
        transport.responses.extend(
            [(201, {"ETag": ETAG, "x-ms-version-id": NATIVE}), (200, blob_headers(plan))] + blob_reads(plan)
        )
        sources.prepare_large(CONTEXT, plan)
        transport.responses.extend(blob_reads(plan))
        sources.commit(CONTEXT, plan)
        transport.responses.extend(blob_reads(plan))
        transport.expire_on_get = True
        with pytest.raises(Problem) as expired:
            sources.read_exact(CONTEXT, plan.reference)
        assert code(expired) == "SOURCE_EXPIRED"
        assert all(request.method != "DELETE" for request in transport.requests)
