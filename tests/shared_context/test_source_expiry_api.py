"""HTTP source expiry with the real UTC source clock; JWT/storage remain offline."""

import hashlib
import time
from datetime import datetime, timedelta, timezone

import pytest

from shared_context import behavior_wire as wire
from test_behavior_api import BASE
from test_behavior_delivery import NOW
from test_behavior_text_api import ORIGINAL, h, source_command
from test_m3_auth import jwk_client, signing_key


def test_HTTP_expiring_source_invalidates_nonexpiring_claim_and_context_on_real_clock(h):
    expiry = datetime.now(timezone.utc) + timedelta(seconds=2)
    command = {**source_command().model_dump(mode="json"), "expires_at": expiry.isoformat()}
    prepared = h.post("/sources/metrics/prepare", command)
    assert prepared.status_code == 200, prepared.text
    assert h.post("/sources/metrics/commit", wire.Command(request_id=command["request_id"])).status_code == 200
    ref = wire.ExactSourceRef(**{
        key: prepared.json()["reference"][key] for key in ("source_id", "version", "sha256", "byte_length")
    })
    draft = wire.ClaimDraft(
        request_id="source-expiry-review", version=1, text="The accepted metrics source has been reviewed.",
        supporting=(ref,), effective_from=NOW,
    )
    preview = h.post("/claims/claim-one/preview", draft)
    assert preview.status_code == 200, preview.text
    review = wire.ClaimReview(
        **draft.model_dump(), decision_id="source-expiry-human",
        **{key: preview.json()[key] for key in ("candidate_sha256", "evidence_sha256", "policy_version")},
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    )
    assert h.post("/claims/claim-one/review", review).status_code == 200
    assert h.post("/claims/claim-one/commit", wire.Command(request_id=draft.request_id)).status_code == 200
    before = h.client.get(BASE + "/sources/metrics/versions/1", headers=h.headers())
    assert before.status_code == 200 and before.content == ORIGINAL
    assert h.post("/context", wire.ContextRequest(request_id="before-expiry", required_claim_ids=("claim-one",))).status_code == 200
    time.sleep(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.02)
    assert datetime.now(timezone.utc) >= expiry
    for suffix in ("/sources/metrics/versions/1", "/claims/claim-one"):
        expired = h.client.get(BASE + suffix, headers=h.headers())
        assert expired.status_code == 409 and expired.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
        assert ORIGINAL not in expired.content
    after = h.post("/context", wire.ContextRequest(request_id="after-expiry", required_claim_ids=("claim-one",)))
    assert after.status_code == 409 and "originals" not in after.json()
    assert h.post("/claims/claim-one/preview", draft).status_code == 409
    assert h.post("/sources/metrics/reconcile", wire.Command(request_id=command["request_id"])).status_code == 200
    for replacement in (None, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()):
        changed = h.post("/sources/metrics/prepare", {**command, "expires_at": replacement})
        assert changed.status_code == 409


def test_default_and_explicit_null_source_DTO_keep_original_wire_digest():
    implicit = wire.SourcePrepare(request_id="expiry-legacy", version=1, original=b"legacy-original-v1")
    explicit = wire.SourcePrepare(request_id="expiry-legacy", version=1, original=b"legacy-original-v1", expires_at=None)
    assert implicit.model_dump_json() == explicit.model_dump_json()
    assert "expires_at" not in explicit.model_dump()
    assert hashlib.sha256(explicit.model_dump_json().encode()).hexdigest() == (
        "653211a0bf28d16dec0f62a8b2fe0fcd841194291cd4a8f478f654a0d84c2080"
    )


def test_HTTP_null_expiry_reconciles_an_existing_legacy_source_prepare(h):
    command = source_command()
    first = h.post("/sources/metrics/prepare", command)
    explicit = h.post("/sources/metrics/prepare", {**command.model_dump(mode="json"), "expires_at": None})
    assert first.status_code == explicit.status_code == 200
    assert first.json()["control_request_digest"] == explicit.json()["control_request_digest"]
    assert h.post("/sources/metrics/commit", wire.Command(request_id=command.request_id)).status_code == 200
    assert h.client.get(BASE + "/sources/metrics/versions/1", headers=h.headers()).content == ORIGINAL


@pytest.mark.parametrize("field", ["clock", "now", "observed_at"])
def test_HTTP_source_expiry_does_not_accept_a_client_clock(h, field):
    body = {**source_command().model_dump(mode="json"),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            field: "2000-01-01T00:00:00Z"}
    assert h.post("/sources/metrics/prepare", body).status_code == 422


@pytest.mark.parametrize("expiry", ["2040-01-01T00:00:00", "2040-01-01T08:00:00+08:00"])
def test_HTTP_source_expiry_rejects_non_UTC_times(h, expiry):
    assert h.post("/sources/metrics/prepare", {
        **source_command().model_dump(mode="json"), "expires_at": expiry,
    }).status_code == 422


def test_HTTP_source_expiry_rejects_past_deadline_using_server_clock(h):
    assert h.post("/sources/metrics/prepare", {
        **source_command().model_dump(mode="json"),
        "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    }).status_code == 409
