"""Read-only team skill review list: current states and hashes, never skill bodies."""

import base64
import copy
import hashlib
import json
from unittest.mock import patch

import httpx
import pytest
from azure.data.tables import TableClient

from conftest import A, C, TENANT
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import (
    MAX_ARTIFACT_VERSIONS, MAX_SKILL_REVIEW_ENTRIES, ApprovalInput, CreateArtifact, SkillDeactivationInput,
    SkillReviewEntry, SkillReviewList, SkillReviewVersion, VersionInput,
)
from shared_context.storage import TableRepository
from test_adapters import Credential, WireTransport
from test_m3_artifacts import HUMAN, V1, V2, m3

BODY = b"SKILL_REVIEW_BODY_CANARY: follow the reviewed receipt.\n"
DEACTIVATION = "a" * 32
# artifact -> (status, latest decision, effective version, deactivated versions)
EXPECTED = {
    "awaiting": ("awaiting_decision", None, None, ()),
    "deactivated": ("deactivated", "approve", None, (V1,)),
    "effective": ("effective", "approve", V1, ()),
    "empty": ("no_version", None, None, ()),
    "pending": ("upload_pending", None, None, ()),
    "redraft": ("awaiting_decision", None, None, (V1,)),
    "rejected-update": ("rejected", "reject", V1, ()),
}


def add_version(service, name, version_id=V1, body=BODY):
    current = service.get(A, A.team, name)
    metadata = VersionInput(filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(body).hexdigest())
    return service.upload(A, A.team, name, version_id, current.etag, metadata, body)


def decide(service, name, version_id, choice):
    current = service.get(HUMAN, A.team, name)
    version = current.payload["versions"][-1]
    return service.decide(HUMAN, A.team, name, current.etag, ApprovalInput(
        decision_id=version_id, version_id=version_id, sha256=version["sha256"], decision=choice,
    ))


def deactivate(service, name):
    approved = decide(service, name, V1, "approve")
    version = approved.payload["versions"][-1]
    service.deactivate_skill(HUMAN, A.team, name, approved.etag, SkillDeactivationInput(
        schema_version="fa-skill-deactivation-v1", decision_id=DEACTIVATION, version_id=V1, sha256=version["sha256"],
    ))


def artifact(service, name, kind="skill"):
    return service.create(A, A.team, name, CreateArtifact(kind=kind, title="Guide " + name, source_work="source-work"))


def review_fixture(service, store):
    for name in EXPECTED:
        artifact(service, name)
    add_version(service, "awaiting")
    add_version(service, "effective")
    decide(service, "effective", V1, "approve")
    add_version(service, "rejected-update")
    decide(service, "rejected-update", V1, "approve")
    add_version(service, "rejected-update", V2)
    decide(service, "rejected-update", V2, "reject")
    add_version(service, "deactivated")
    deactivate(service, "deactivated")
    add_version(service, "redraft")
    deactivate(service, "redraft")
    add_version(service, "redraft", V2)
    with patch.object(store, "put", side_effect=Problem(503, "BLOB_UNAVAILABLE", "synthetic outage")):
        with pytest.raises(Problem):
            add_version(service, "pending")
    for name in ("0-report", "1-report", "z-report"):
        artifact(service, name, "deliverable")
    artifact(service, "team-sop", "sop")


def observed(entries):
    return {entry.artifact_id: (entry.status, None if entry.latest_version is None else entry.latest_version.decision,
                                entry.effective_version_id, entry.deactivated_version_ids) for entry in entries}


def test_review_list_reports_each_skill_state_without_bodies_or_writes(m3):
    service, repository, store = m3
    review_fixture(service, store)
    before = copy.deepcopy(repository.data)
    for principal in (HUMAN, A):
        with patch.object(store, "read", side_effect=AssertionError("the review list must not read skill bodies")):
            listed = service.skill_reviews(principal, A.team)
        assert "SKILL_REVIEW_BODY_CANARY" not in listed.model_dump_json()
        assert listed.team == A.team and listed.after is None and listed.next_after is None
        assert observed(listed.entries) == EXPECTED
        latest = {entry.artifact_id: entry.latest_version for entry in listed.entries}
        assert latest["rejected-update"].version_id == V2 and latest["rejected-update"].number == 2
        assert latest["redraft"].version_id == V2
        assert latest["effective"].sha256 == hashlib.sha256(BODY).hexdigest()
        assert latest["empty"] is None and latest["pending"] is None
    assert repository.data == before


def test_pages_cover_every_skill_once_even_when_deliverables_fill_a_page(m3, monkeypatch):
    service, _, store = m3
    review_fixture(service, store)
    monkeypatch.setattr("shared_context.m3_artifacts.MAX_SKILL_REVIEW_ENTRIES", 2)
    pages, after = [], None
    while True:
        page = service.skill_reviews(HUMAN, A.team, after)
        assert page.after == after and len(page.entries) <= 2
        pages.append(page)
        if page.next_after is None:
            break
        after = page.next_after
    assert any(not page.entries and page.next_after for page in pages)
    entries = [entry for page in pages for entry in page.entries]
    assert [entry.artifact_id for entry in entries] == sorted(EXPECTED)
    assert observed(entries) == EXPECTED
    with pytest.raises(Problem) as invalid:
        service.skill_reviews(HUMAN, A.team, "../x")
    assert invalid.value.status == 422


def test_review_list_keeps_existing_team_authorization(m3):
    service, _, _ = m3
    artifact(service, "guide")
    assert [entry.artifact_id for entry in service.skill_reviews(HUMAN, A.team).entries] == ["guide"]
    assert [entry.artifact_id for entry in service.skill_reviews(A, A.team).entries] == ["guide"]
    with pytest.raises(Problem) as other_team:
        service.skill_reviews(C, A.team)
    assert other_team.value.status == 403
    with pytest.raises(Problem) as denied:
        service.skill_reviews(HumanApprover(HUMAN.oid, C.team), C.team)
    assert denied.value.status == 403 and denied.value.body["error"]["code"] == "M3_TEAM_DENIED"


@pytest.mark.parametrize("change", ["unknown-field", "row-identity"])
def test_review_list_refuses_unverifiable_state(m3, change):
    service, repository, _ = m3
    artifact(service, "guide")
    key = ("m3-" + TENANT + "-" + A.team, "artifact:guide")
    record = repository.data[key]
    if change == "unknown-field":
        record.payload["unexpected"] = True
    else:
        repository.data[key[0], "artifact:copy"] = copy.deepcopy(record)
    with pytest.raises(Problem) as denied:
        service.skill_reviews(HUMAN, A.team)
    assert denied.value.status == 502 and denied.value.body["error"]["code"] == "M3_STATE_INVALID"


def test_worst_case_page_fits_one_64k_response():
    version = SkillReviewVersion(version_id=V1, number=MAX_ARTIFACT_VERSIONS, sha256="a" * 64,
                                 metadata_sha256="b" * 64, created_at="x" * 64, decision="approve")
    entries = tuple(SkillReviewEntry(
        artifact_id=f"{index:02d}" + "a" * 62, title="審" * 256, owner="o" * 64, source_work="w" * 64,
        revision=10 ** 9, status="awaiting_decision", latest_version=version, effective_version_id=V2,
        deactivated_version_ids=tuple(f"{item:032x}" for item in range(MAX_ARTIFACT_VERSIONS)),
    ) for index in range(MAX_SKILL_REVIEW_ENTRIES))
    page = SkillReviewList(team="t" * 64, after="0" * 64, entries=entries, next_after="z" * 64)
    assert len(json.dumps(page.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")) < 64 * 1024


def table(responses):
    transport = WireTransport(responses)
    return transport, TableClient("https://synthetic.table.core.windows.net", "PocTable",
                                  credential=Credential(), transport=transport, retry_total=0)


def test_table_repository_reads_one_bounded_row_range_page():
    payload = {"kind": "skill"}
    entity = {"PartitionKey": "m3-team", "RowKey": "artifact:guide", "odata.etag": 'W/"guide"',
              "Payload@odata.type": "Edm.Binary", "Payload": base64.b64encode(json.dumps(payload).encode()).decode()}
    transport, client_ = table([(200, {"value": [entity]}, {})])
    rows, more = TableRepository(client_).list_prefix("m3-team", "artifact:", 3)
    assert [(row, record.payload, record.etag) for row, record in rows] == [("artifact:guide", payload, 'W/"guide"')]
    assert more is False
    query = httpx.URL(transport.requests[0].url).params
    assert query["$filter"] == "PartitionKey eq 'm3-team' and RowKey ge 'artifact:' and RowKey lt 'artifact;'"
    assert query["$select"] == "RowKey,Payload" and query["$top"] == "4"
    transport, client_ = table([(200, {"value": [entity, {**entity, "RowKey": "artifact:other"}]}, {})])
    rows, more = TableRepository(client_).list_prefix("m3-team", "artifact:", 1, after="artifact:earlier")
    assert [row for row, _ in rows] == ["artifact:guide"] and more is True
    query = httpx.URL(transport.requests[0].url).params
    assert query["$filter"] == "PartitionKey eq 'm3-team' and RowKey gt 'artifact:earlier' and RowKey lt 'artifact;'"
    with pytest.raises(ValueError):
        TableRepository(client_).list_prefix("m3-team", "artifact:", 1, after="work:outside")
