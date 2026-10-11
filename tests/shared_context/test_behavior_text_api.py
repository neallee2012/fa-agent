"""Signed-human exact-text intake and reviewed claims; no native/model proof."""
import base64
import hashlib

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_runtime import runtime_key
from test_behavior_api import BASE, RuntimeHarness
from test_behavior_delivery import NOW
from test_m3_auth import jwk_client, signing_key

ORIGINAL = "Project Lyra 的報告採 compact。\r\n我的介面偏好是 cobalt。\n".encode("utf-8")


@pytest.fixture
def h(auth, jwk_client, signing_key):
    harness = RuntimeHarness(auth, jwk_client, signing_key)
    harness.mutate(runtime_key("policy", "binding-one"), human_text_enabled=True, text_policy_version="human-text-policy-v1")
    return harness


def source_command(content=ORIGINAL):
    return wire.SourcePrepare(
        request_id="human-original", version=1, original=content, source_schema="fa-human-text-v1",
        retention_consent_sha256=hashlib.sha256(content).hexdigest(),
    )


def test_human_original_claim_and_context_keep_exact_bytes_and_live_withdrawal(h):
    result = h.post("/sources/metrics/prepare", source_command())
    assert result.status_code == 200, result.text
    assert h.post("/sources/metrics/commit", wire.Command(request_id="human-original")).status_code == 200
    original = h.client.get(BASE + "/sources/metrics/versions/1", headers=h.headers())
    assert original.status_code == 200 and original.content == ORIGINAL
    assert original.headers["content-type"].startswith("text/plain")
    ref = wire.ExactSourceRef(source_id="metrics", version=1, sha256=hashlib.sha256(ORIGINAL).hexdigest(), byte_length=len(ORIGINAL))
    draft = wire.ClaimDraft(request_id="claim-review", version=1, text="使用者的介面偏好是 cobalt。",
                            supporting=(ref,), effective_from=NOW)
    preview = h.post("/claims/claim-one/preview", draft)
    assert preview.status_code == 200, preview.text
    review = wire.ClaimReview(
        **draft.model_dump(), decision_id="explicit-human-review",
        **{key: preview.json()[key] for key in ("candidate_sha256", "evidence_sha256", "policy_version")},
        reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
    )
    assert h.post("/claims/claim-one/review", review).status_code == 200
    assert h.post("/claims/claim-one/commit", wire.Command(request_id="claim-review")).status_code == 200
    context = h.post("/context", wire.ContextRequest(request_id="read-context", required_claim_ids=("claim-one",)))
    assert context.status_code == 200, context.text
    assert ORIGINAL in [base64.b64decode(item) for item in context.json()["originals"].values()]
    assert "cobalt" in context.text
    assert h.post("/sources/metrics/withdraw", wire.SourceWithdrawal(
        request_id="withdraw", withdrawal_id="withdrawn", reference=ref, expected_access_revision=1,
    )).status_code == 200
    assert h.post("/context", wire.ContextRequest(request_id="late-context", required_claim_ids=("claim-one",))).status_code == 403


@pytest.mark.parametrize("principal,changes", [
    ("agent", {}), ("human", {"retention_consent_sha256": None}), ("human", {"retention_consent_sha256": "f" * 64}),
])
def test_text_needs_signed_human_and_exact_retention_consent(h, principal, changes):
    value = source_command().model_copy(update=changes)
    assert h.post("/sources/metrics/prepare", value, identity=principal).status_code == 403


@pytest.mark.parametrize("content", [
    b"Bearer do-not-store", b"password=secret-value", b'client_secret="secret-value"',
    b"-----BEGIN PRIVATE KEY-----", b"ghp_" + b"x" * 32, b"null\x00byte", b"\xff\xfe",
])
def test_known_credential_and_invalid_text_rejected_before_plan_storage(h, content):
    count = len(h.transport.backing.rows)
    result = h.post("/sources/metrics/prepare", source_command(content))
    assert result.status_code == 422
    assert content.decode("utf-8", errors="replace") not in result.text
    assert len(h.transport.backing.rows) == count


def test_disabled_server_text_policy_rejects_even_exact_human_consent(h):
    h.mutate(runtime_key("policy", "binding-one"), human_text_enabled=False, text_policy_version=None)
    assert h.post("/sources/metrics/prepare", source_command()).status_code == 403


def test_revoked_text_policy_blocks_prepared_commit_without_rewriting_original(h):
    assert h.post("/sources/metrics/prepare", source_command()).status_code == 200
    h.mutate(runtime_key("policy", "binding-one"), human_text_enabled=False, text_policy_version=None)
    assert h.post("/sources/metrics/commit", wire.Command(request_id="human-original")).status_code == 403


def test_arbitrary_claim_text_is_not_supported_without_explicit_server_policy(h):
    h.mutate(runtime_key("policy", "binding-one"), human_text_enabled=False, text_policy_version=None)
    ref = h.source()
    draft = wire.ClaimDraft(request_id="unreviewed", version=1, text="Do not treat caller text as policy.",
                            supporting=(ref,), effective_from=NOW)
    assert h.post("/claims/claim-one/preview", draft).status_code == 422
