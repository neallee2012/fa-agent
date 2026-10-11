from __future__ import annotations

import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from conftest import A, B, C, TENANT
from shared_context.errors import Problem
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings, HumanApprover
from shared_context.m3_models import (
    MAX_ARTIFACT_BYTES, ApprovalInput, ApprovalRecord, ArtifactDocument, ArtifactVersion,
    BlobReceipt, CreateArtifact, VersionInput,
)
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from shared_context.storage import MAX_PAYLOAD_BYTES, serialize

HUMAN = HumanApprover("dddddddd-dddd-dddd-dddd-dddddddddddd", A.team)
SETTINGS = ApprovalSettings(HUMAN.oid, "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", A.team)
V1, V2, V3 = "1" * 32, "2" * 32, "3" * 32


@pytest.fixture
def m3(system):
    context, repository, _, _ = system
    context.create(A, A.team, "source-work", CreateWork(goal="Synthetic M3 source"))
    store = InMemoryArtifactStore(TENANT)
    return ArtifactService(context, store, SETTINGS), repository, store


def create(service, kind="deliverable"):
    return service.create(A, A.team, "report", CreateArtifact(kind=kind, title="Synthetic report", source_work="source-work"))


def upload(service, version_id=V1, data=b"report v1\n", *, etag=None):
    before = service.get(A, A.team, "report")
    metadata = VersionInput(filename="report.txt", media_type="text/plain", sha256=hashlib.sha256(data).hexdigest())
    return service.upload(A, A.team, "report", version_id, etag or before.etag, metadata, data)


def decision(service, version_id=V1, choice="approve", *, principal=HUMAN, etag=None, decision_id=None, digest=None):
    before = service.get(principal, A.team, "report")
    version = next(item for item in before.payload["versions"] if item["version_id"] == version_id)
    return service.decide(principal, A.team, "report", etag or before.etag, ApprovalInput(
        version_id=version_id, decision_id=decision_id or version_id, decision=choice, sha256=digest or version["sha256"],
    ))


def code(error):
    return error.value.body["error"]["code"]


def test_real_file_versions_and_published_legacy_work_remain_unchanged(m3):
    service, repository, store = m3
    work = service.context.get(A, A.team, "source-work")
    service.context.publish(A, A.team, "source-work", work.etag)
    original = copy.deepcopy(repository.data[A.team, "work:source-work"])
    create(service)
    first = upload(service)
    upload(service, V2, b"report v2\n")
    assert service.get(HUMAN, A.team, "report").payload["versions"][0] == first.payload["versions"][0]
    for version_id, expected in ((V1, b"report v1\n"), (V2, b"report v2\n")):
        receipt = service.version(B, A.team, "report", version_id)
        _, data = service.download(HUMAN, A.team, "report", version_id, receipt.blob.etag)
        assert data == expected
    assert repository.data[A.team, "work:source-work"] == original
    assert all(partition.startswith("m3-" + TENANT) or partition == A.team for partition, _ in repository.data)
    with patch.object(store, "put", wraps=store.put) as put:
        repeat = upload(service, V1, etag='"stale-but-already-completed"')
    assert repeat == service.get(A, A.team, "report")
    put.assert_not_called()


@pytest.mark.parametrize("kind", ["sop", "skill"])
def test_exact_human_approval_and_rejection_preserve_previous_effective_version(m3, kind):
    service, _, _ = m3
    create(service, kind)
    upload(service)
    assert service.policy_context(A, A.team, ("report",)) == [{"artifact_id": "report", "status": "not_approved"}]
    with pytest.raises(Problem) as exc:
        service.effective(A, A.team, "report")
    assert code(exc) == "M3_NO_EFFECTIVE_VERSION"
    decision(service)
    assert service.effective(A, A.team, "report").version_id == V1
    upload(service, V2, b"unapproved v2")
    assert service.policy_context(B, A.team, ("report",))[0]["text"] == "report v1\n"
    decision(service, V2, "reject")
    assert service.effective(B, A.team, "report").version_id == V1
    upload(service, V3, b"approved v3")
    with pytest.raises(Problem) as exc:
        decision(service, V3, digest="0" * 64)
    assert code(exc) == "M3_APPROVAL_HASH_MISMATCH"
    accepted = decision(service, V3)
    assert decision(service, V3, etag='"stale"') == accepted
    assert service.policy_context(A, A.team, ("report",))[0]["text"] == "approved v3"
    assert service.effective(A, A.team, "report").approval.approver_oid == HUMAN.oid
    with pytest.raises(Problem) as exc:
        decision(service, V3, choice="reject")
    assert code(exc) == "M3_DECISION_CONFLICT"


def test_roles_owner_and_team_are_not_request_claims(m3):
    service, _, _ = m3
    create(service, "sop")
    upload(service)
    with pytest.raises(Problem) as exc:
        decision(service, principal=A)
    assert code(exc) == "M3_HUMAN_REQUIRED"
    for principal, team in ((C, A.team), (A, C.team), (HumanApprover(B.oid, A.team), A.team)):
        with pytest.raises(Problem):
            service.get(principal, team, "report")
    assert service.version(B, A.team, "report", V1).number == 1
    with pytest.raises(Problem) as exc:
        service.upload(B, A.team, "report", V2, service.get(B, A.team, "report").etag,
                       VersionInput(filename="peer.txt", media_type="text/plain", sha256=hashlib.sha256(b"x").hexdigest()), b"x")
    assert code(exc) == "M3_NOT_OWNER"
    with pytest.raises(Problem):
        service.create(HUMAN, A.team, "human-file", CreateArtifact(kind="sop", title="forged", source_work="source-work"))


def test_declined_older_proposal_and_ordinary_file_cannot_activate(m3):
    service, _, _ = m3
    create(service)
    upload(service)
    with pytest.raises(Problem) as exc:
        decision(service)
    assert code(exc) == "M3_NOT_POLICY"
    with pytest.raises(Problem):
        service.policy_context(A, A.team, ("report",))


def test_only_latest_proposal_can_receive_a_new_decision(m3):
    service, _, _ = m3
    create(service, "sop")
    upload(service)
    upload(service, V2, b"later proposal")
    with pytest.raises(Problem) as exc:
        decision(service)
    assert code(exc) == "M3_STALE_PROPOSAL"
    rejected = decision(service, V2, "reject")
    with pytest.raises(Problem) as exc:
        decision(service, V2, decision_id=V3)
    assert code(exc) == "M3_ALREADY_DECIDED"
    assert service.get(HUMAN, A.team, "report") == rejected


@pytest.mark.parametrize("bad_etag", [None, "*", '"wrong"', '"one","two"', "bad\netag"])
def test_upload_requires_exact_etag_without_blob_side_effects(m3, bad_etag):
    service, _, store = m3
    create(service)
    with patch.object(store, "put", wraps=store.put) as put, pytest.raises(Problem):
        service.upload(A, A.team, "report", V1, bad_etag,
                       VersionInput(filename="r.txt", media_type="text/plain", sha256=hashlib.sha256(b"x").hexdigest()), b"x")
    put.assert_not_called()


def test_unknown_blob_write_is_reconciled_without_resending_bytes(m3):
    service, _, store = m3
    create(service, "sop")
    native_put = store.put

    def lost_ack(*args):
        native_put(*args)
        raise Problem(502, "SYNTHETIC_LOST_ACK", "simulated accepted upload with lost acknowledgement")

    with patch.object(store, "put", side_effect=lost_ack) as put:
        with pytest.raises(Problem):
            upload(service)
        pending = service.get(A, A.team, "report")
        assert pending.payload["pending"]["version_id"] == V1
        with pytest.raises(Problem) as exc:
            upload(service)
        assert code(exc) == "M3_UPLOAD_UNKNOWN"
        put.assert_called_once()
        with pytest.raises(Problem):
            service.download(HUMAN, A.team, "report", V1, '"anything"')
        completed = service.reconcile(A, A.team, "report", V1, pending.etag)
        assert completed.payload["pending"] is None
        assert completed.payload["versions"][0]["blob"]["sha256"] == hashlib.sha256(b"report v1\n").hexdigest()
        put.assert_called_once()


def test_unknown_final_table_commit_recovers_by_reading_completed_receipt(m3):
    service, repository, store = m3
    create(service)
    native_replace = repository.replace

    def lost_commit(team, row, payload, etag):
        result = native_replace(team, row, payload, etag)
        if payload.get("versions"):
            raise Problem(502, "SYNTHETIC_LOST_ACK", "simulated final commit acknowledgement loss")
        return result

    with patch.object(repository, "replace", side_effect=lost_commit), patch.object(store, "put", wraps=store.put) as put:
        with pytest.raises(Problem):
            upload(service)
        assert upload(service).payload["versions"][0]["version_id"] == V1
        put.assert_called_once()


def test_failed_reservation_does_not_upload_or_overwrite(m3):
    service, repository, store = m3
    create(service)
    with patch.object(repository, "replace", side_effect=Problem(502, "SYNTHETIC_FAILURE", "reservation failed")):
        with patch.object(store, "put", wraps=store.put) as put, pytest.raises(Problem):
            upload(service)
    put.assert_not_called()


def test_unknown_upload_without_a_blob_stays_blocked_without_resend(m3):
    service, _, store = m3
    create(service)
    with patch.object(store, "put", side_effect=Problem(502, "SYNTHETIC_FAILURE", "no acknowledgement")) as put:
        with pytest.raises(Problem):
            upload(service)
        pending = service.get(A, A.team, "report")
        with pytest.raises(Problem):
            service.reconcile(A, A.team, "report", V1, pending.etag)
        with pytest.raises(Problem):
            upload(service)
        put.assert_called_once()
    assert service.get(A, A.team, "report") == pending


def test_thirteenth_version_is_rejected_before_any_blob_write(m3):
    service, _, store = m3
    create(service)
    for index in range(1, 13):
        upload(service, version_id=f"{index:032x}")
    complete = service.get(A, A.team, "report")
    with patch.object(store, "put", wraps=store.put) as put, pytest.raises(Problem) as exc:
        upload(service, version_id=f"{13:032x}")
    assert code(exc) == "M3_VERSION_LIMIT"
    put.assert_not_called()
    assert service.get(A, A.team, "report") == complete


def test_concurrent_proposals_do_not_create_two_blobs(m3):
    service, _, store = m3
    initial = create(service)

    def attempt(version):
        try:
            upload(service, version, etag=initial.etag)
            return "accepted"
        except Problem as exc:
            return exc.status

    with patch.object(store, "put", wraps=store.put) as put, ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (V1, V2)))
    assert results.count("accepted") == 1
    put.assert_called_once()
    assert len(service.get(A, A.team, "report").payload["versions"]) == 1


def test_approval_cas_cannot_activate_a_superseded_snapshot(m3):
    service, _, _ = m3
    create(service, "sop")
    first = upload(service)
    upload(service, V2, b"later")
    with pytest.raises(Problem) as exc:
        decision(service, V2, etag=first.etag)
    assert code(exc) == "ETAG_CONFLICT"


def test_hash_corruption_and_missing_policies_are_not_success_shaped(m3):
    service, _, store = m3
    create(service, "sop")
    upload(service)
    with patch.object(store, "read", return_value=b"bad bytes"):
        with pytest.raises(Problem) as exc:
            decision(service)
        assert code(exc) == "M3_FILE_INTEGRITY"
    assert service.get(HUMAN, A.team, "report").payload["effective_version_id"] is None
    with pytest.raises(Problem):
        service.policy_context(A, A.team, ("missing-policy",))


def test_each_revision_can_pin_its_own_real_source_work(m3):
    service, _, _ = m3
    create(service)
    upload(service)
    service.context.create(A, A.team, "revision-work", CreateWork(goal="Create a new version without changing the original work"))
    service.context.create(B, A.team, "peer-work", CreateWork(goal="Not owned by the submitting agent"))
    initial = service.get(A, A.team, "report")
    body = VersionInput(filename="v2.txt", media_type="text/plain", sha256=hashlib.sha256(b"v2").hexdigest(),
                        source_work="peer-work")
    with pytest.raises(Problem) as exc:
        service.upload(A, A.team, "report", V2, initial.etag, body, b"v2")
    assert code(exc) == "NOT_OWNER"
    body.source_work = "revision-work"
    saved = service.upload(A, A.team, "report", V2, initial.etag, body, b"v2")
    assert [version["source_work"] for version in saved.payload["versions"]] == ["source-work", "revision-work"]
    assert saved.payload["source_work"] == "source-work"


@pytest.mark.parametrize("data", [b"", b"x" * (MAX_ARTIFACT_BYTES + 1)], ids=["empty", "over-512k"])
def test_file_size_limits_precede_storage(m3, data):
    service, _, store = m3
    create(service)
    with patch.object(store, "put", wraps=store.put) as put, pytest.raises(Problem):
        upload(service, data=data)
    put.assert_not_called()


@pytest.mark.parametrize("data", [b"\xff", b"x" * 8193, b"\x1b[2Jhide prompt", b" \n"],
                         ids=["invalid-utf8", "over-8k", "terminal-escape", "whitespace"])
def test_policy_format_is_bounded_visible_utf8(m3, data):
    service, _, store = m3
    create(service, "skill")
    with patch.object(store, "put", wraps=store.put) as put, pytest.raises(Problem):
        upload(service, data=data)
    put.assert_not_called()


def test_worst_case_supported_history_fits_authoritative_table_limit():
    versions = []
    for index in range(1, 13):
        version_id = f"{index:032x}"
        digest = f"{index:064x}"
        versions.append(ArtifactVersion(
            filename="x" * 128, media_type="application/octet-stream", sha256=digest,
            version_id=version_id, number=index, created_by_oid=A.oid, created_at="t" * 64,
            source_work_revision=999999999999,
            source_work="w" * 64,
            blob=BlobReceipt(native_version="v" * 256, etag='"' + "e" * 254 + '"', sha256=digest, bytes=MAX_ARTIFACT_BYTES),
            approval=ApprovalRecord(version_id=version_id, decision_id=version_id, sha256=digest, decision="approve",
                                    approver_oid=HUMAN.oid, decided_at="t" * 64),
        ))
    document = ArtifactDocument(
        kind="sop", title="\u6f22" * 256, source_work="w" * 64, artifact_id="a" * 64, team="t" * 64,
        owner="o" * 64, owner_oid=A.oid, revision=999999999999, versions=versions, effective_version_id=versions[-1].version_id,
    )
    assert len(serialize(document.model_dump())) < MAX_PAYLOAD_BYTES
