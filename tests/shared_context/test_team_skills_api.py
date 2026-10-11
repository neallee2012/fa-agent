"""Behavior API owner routes for reviewing Team skills through the existing M3 store."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A, C, TENANT, FakeMemory, InMemoryRepository
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier, BehaviorPrincipal
from shared_context.behavior_store import BehaviorStore
from shared_context.behavior_team_registry import TeamRegistry
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_auth import ApprovalSettings
from shared_context.m3_models import (
    CreateArtifact, SkillPurpose, SkillResourceReference, SkillResources, VersionInput,
)
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.models import CreateWork
from shared_context.service import ContextService
from shared_context.team_skills_api import PREFIX, install_team_skills_routes
from test_behavior_store import FakeTable, SCOPE
from test_m3_auth import CLIENT_ID, HUMAN_OID, jwk_client, signing_key, token

SECOND_OWNER = "99999999-9999-9999-9999-999999999999"
SKILL = "review-skill"
RESOURCE = "review-resource"
NON_SKILL = "ordinary-file"
V1 = "1" * 32
V2 = "2" * 32
RV1 = "a" * 32
SKILL_TEXT = "Read the reviewed draft and report the receipt.\n"
RESOURCE_TEXT = "Attachment data visible before approval.\n"


@pytest.fixture
def team_skills(auth, jwk_client, signing_key):
    context = ContextService(InMemoryRepository(), FakeMemory(), auth)
    context.create(A, A.team, "source-work", CreateWork(goal="Synthetic skill review source"))
    service = ArtifactService(
        context, InMemoryArtifactStore(TENANT), ApprovalSettings(HUMAN_OID, CLIENT_ID, A.team))

    table = FakeTable()
    behavior_store = BehaviorStore(table, SCOPE)
    behavior_store.initialize()
    teams = TeamRegistry(
        behavior_store, tenant_id=TENANT, operator_oid=HUMAN_OID, legacy_team_id=A.team,
        legacy_owner_oid=HUMAN_OID, agent_oids=tuple(auth.actors))
    operator = BehaviorPrincipal(TENANT, HUMAN_OID, A.team, "human", None, frozenset({"read", "submit", "approve"}))
    teams.register_owner(operator, "second-owner", C.team, SECOND_OWNER)
    verifier = BehaviorJwtVerifier(
        auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client, teams=teams)
    app = FastAPI()
    _install_error_handlers(app)
    install_team_skills_routes(app, SimpleNamespace(
        verifier=verifier, assistant=SimpleNamespace(authority=SimpleNamespace(artifacts=service))))

    def headers(identity: str = "owner"):
        if identity == "agent":
            claims = {"oid": A.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}
            return {"Authorization": "Bearer " + token(signing_key, claims, remove=("scp",)),
                    "Content-Type": "application/json"}
        scopes = {
            "owner": "Behavior.Read Behavior.Approve",
            "read_only": "Behavior.Read",
            "other_human": "Behavior.Read Behavior.Approve",
        }[identity]
        oid = SECOND_OWNER if identity == "other_human" else HUMAN_OID
        return {"Authorization": "Bearer " + token(signing_key, {"oid": oid, "scp": scopes}),
                "Content-Type": "application/json"}

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, service=service, headers=headers)


def upload_skill(service, artifact=SKILL, version=V1, text=SKILL_TEXT, *, with_resource=True):
    created = service.create(A, A.team, artifact, CreateArtifact(
        kind="skill", title="Reviewable skill", source_work="source-work"))
    resources = None
    if with_resource:
        resource = service.create(A, A.team, RESOURCE, CreateArtifact(
            kind="deliverable", title="Review resource", source_work="source-work"))
        raw_resource = RESOURCE_TEXT.encode("utf-8")
        resource_version = service.upload(A, A.team, RESOURCE, RV1, resource.etag, VersionInput(
            filename="resource.md", media_type="text/markdown",
            sha256=hashlib.sha256(raw_resource).hexdigest()), raw_resource)
        resources = SkillResources(entries=[SkillResourceReference(
            name="resource.md", artifact_id=RESOURCE, version_id=RV1,
            sha256=resource_version.payload["versions"][0]["sha256"])])
    raw = text.encode("utf-8")
    metadata = VersionInput(
        filename="skill.md", media_type="text/markdown", sha256=hashlib.sha256(raw).hexdigest(),
        skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description="Review the saved procedure."),
        skill_resources=resources,
    )
    return service.upload(A, A.team, artifact, version, created.etag, metadata, raw)


def decision_body(version, *, decision_id="d" * 32, decision="approve"):
    return {
        "decision_id": decision_id, "version_id": version["version_id"],
        "sha256": version["sha256"], "metadata_sha256": version.get("metadata_sha256"),
        "decision": decision,
    }


def deactivate_body(version, *, decision_id="e" * 32):
    return {
        "schema_version": "fa-skill-deactivation-v1", "decision_id": decision_id,
        "version_id": version["version_id"], "sha256": version["sha256"],
        "metadata_sha256": version.get("metadata_sha256"),
    }


def post_decision(c, record, body, *, identity="owner", etag=None):
    headers = c.headers(identity)
    if etag is not None:
        headers["If-Match"] = etag
    return c.client.post(PREFIX + f"/skills/{SKILL}/decisions", headers=headers, json=body)


def test_owner_reads_reviews_artifact_version_content_and_pending_resource(team_skills):
    c = team_skills
    uploaded = upload_skill(c.service)
    version = uploaded.payload["versions"][0]

    reviews = c.client.get(PREFIX + "/reviews", headers=c.headers())
    assert reviews.status_code == 200, reviews.text
    assert reviews.headers["cache-control"] == "no-store"
    assert reviews.json()["entries"][0]["status"] == "awaiting_decision"

    artifact = c.client.get(PREFIX + f"/skills/{SKILL}", headers=c.headers())
    assert artifact.status_code == 200, artifact.text
    assert artifact.headers["etag"] == uploaded.etag
    assert artifact.json()["etag"] == uploaded.etag
    assert artifact.json()["kind"] == "skill"

    version_read = c.client.get(PREFIX + f"/skills/{SKILL}/versions/{V1}", headers=c.headers())
    assert version_read.status_code == 200, version_read.text
    assert version_read.json()["version_id"] == V1
    assert version_read.json()["metadata_sha256"] == version["metadata_sha256"]

    content = c.client.get(PREFIX + f"/skills/{SKILL}/versions/{V1}/content", headers=c.headers())
    assert content.status_code == 200, content.text
    assert content.content == SKILL_TEXT.encode("utf-8")
    assert content.headers["x-artifact-sha256"] == version["sha256"]
    assert content.headers["cache-control"] == "no-store"

    resource = c.client.get(PREFIX + f"/skills/{SKILL}/versions/{V1}/resources/resource.md", headers=c.headers())
    assert resource.status_code == 200, resource.text
    assert resource.json() == {
        "schema_version": "fa-team-skill-resource-v1", "artifact_id": SKILL, "version_id": V1,
        "name": "resource.md", "media_type": "text/markdown",
        "sha256": hashlib.sha256(RESOURCE_TEXT.encode("utf-8")).hexdigest(), "text": RESOURCE_TEXT,
    }
    assert c.client.get(PREFIX + f"/skills/{SKILL}/versions/{V1}/resources/missing.md",
                        headers=c.headers()).status_code == 404


def test_read_only_owner_can_read_but_not_post_and_nonowners_are_refused(team_skills):
    c = team_skills
    uploaded = upload_skill(c.service)
    body = decision_body(uploaded.payload["versions"][0])
    assert c.client.get(PREFIX + "/reviews", headers=c.headers("read_only")).status_code == 200

    read_only = post_decision(c, uploaded, body, identity="read_only", etag=uploaded.etag)
    assert read_only.status_code == 403 and read_only.json()["error"]["code"] == "TEAM_SKILLS_OWNER_ONLY"
    for identity in ("agent", "other_human"):
        denied = c.client.get(PREFIX + "/reviews", headers=c.headers(identity))
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "TEAM_SKILLS_OWNER_ONLY"


def test_approve_is_effective_and_decision_id_idempotency_preserves_original_write(team_skills):
    c = team_skills
    uploaded = upload_skill(c.service)
    version = uploaded.payload["versions"][0]
    body = decision_body(version)

    approved = post_decision(c, uploaded, body, etag=uploaded.etag)
    assert approved.status_code == 200, approved.text
    assert approved.headers["etag"] != uploaded.etag
    assert approved.json()["effective_version_id"] == V1

    writes = c.service.repository.sequence
    repeated = post_decision(c, uploaded, body, etag='"stale"')
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == approved.json()
    assert c.service.repository.sequence == writes

    conflict = post_decision(c, uploaded, {**body, "decision": "reject"}, etag=approved.headers["etag"])
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "M3_DECISION_CONFLICT"
    assert c.service.repository.sequence == writes


def test_decision_etag_reject_paths_and_deactivation_review_state(team_skills):
    c = team_skills
    stale = upload_skill(c.service, "stale-skill", "3" * 32, with_resource=False)
    stale_version = stale.payload["versions"][0]
    before = c.service.repository.sequence
    stale_response = c.client.post(PREFIX + "/skills/stale-skill/decisions", headers={
        **c.headers(), "If-Match": '"stale"'}, json=decision_body(stale_version, decision_id="3" * 32))
    assert stale_response.status_code == 412
    assert c.service.repository.sequence == before
    missing = c.client.post(PREFIX + "/skills/stale-skill/decisions", headers=c.headers(),
                            json=decision_body(stale_version, decision_id="4" * 32))
    assert missing.status_code == 428

    rejected = c.client.post(PREFIX + "/skills/stale-skill/decisions", headers={
        **c.headers(), "If-Match": stale.etag},
        json=decision_body(stale_version, decision_id="5" * 32, decision="reject"))
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["effective_version_id"] is None
    assert rejected.json()["versions"][0]["approval"]["decision"] == "reject"

    effective = upload_skill(c.service, with_resource=False)
    version = effective.payload["versions"][0]
    approved = post_decision(c, effective, decision_body(version), etag=effective.etag)
    deactivated = c.client.post(PREFIX + f"/skills/{SKILL}/deactivations", headers={
        **c.headers(), "If-Match": approved.headers["etag"]}, json=deactivate_body(version))
    assert deactivated.status_code == 200, deactivated.text
    assert deactivated.json()["effective_version_id"] is None
    reviews = c.client.get(PREFIX + "/reviews", headers=c.headers()).json()
    entry = next(item for item in reviews["entries"] if item["artifact_id"] == SKILL)
    assert entry["status"] == "deactivated"


def test_non_skill_invalid_requests_and_no_delete_route(team_skills):
    c = team_skills
    created = c.service.create(A, A.team, NON_SKILL, CreateArtifact(
        kind="deliverable", title="Ordinary file", source_work="source-work"))
    raw = b"ordinary\n"
    c.service.upload(A, A.team, NON_SKILL, V2, created.etag, VersionInput(
        filename="ordinary.txt", media_type="text/plain", sha256=hashlib.sha256(raw).hexdigest()), raw)
    non_skill = c.client.get(PREFIX + f"/skills/{NON_SKILL}", headers=c.headers())
    assert non_skill.status_code == 409
    assert non_skill.json()["error"]["code"] == "TEAM_SKILL_NOT_SKILL"

    uploaded = upload_skill(c.service)
    version = uploaded.payload["versions"][0]
    assert c.client.get(PREFIX + "/reviews?after=x&extra=y", headers=c.headers()).status_code == 422
    extra = c.client.post(PREFIX + f"/skills/{SKILL}/decisions", headers={
        **c.headers(), "If-Match": uploaded.etag},
        json={**decision_body(version), "extra": True})
    assert extra.status_code == 422
    assert c.client.delete(PREFIX + f"/skills/{SKILL}", headers=c.headers()).status_code == 405


def test_routes_return_unavailable_when_assistant_authority_is_missing(auth, jwk_client, signing_key):
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, A.team), jwk_client)
    app = FastAPI()
    _install_error_handlers(app)
    install_team_skills_routes(app, SimpleNamespace(verifier=verifier, assistant=None))
    with TestClient(app) as client:
        response = client.get(PREFIX + "/reviews", headers={
            "Authorization": "Bearer " + token(signing_key, {"scp": "Behavior.Read Behavior.Approve"})})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "TEAM_SKILLS_UNAVAILABLE"


def test_stale_approval_does_not_verify_blob_or_write(team_skills):
    c = team_skills
    uploaded = upload_skill(c.service)
    version = uploaded.payload["versions"][0]
    with patch.object(c.service.store, "read", side_effect=AssertionError("stale ETag must stop before blob read")):
        response = post_decision(c, uploaded, decision_body(version, decision_id="f" * 32), etag='"stale"')
    assert response.status_code == 412

OWNER_RESOURCE = "Resource the owner typed in.\n"


def owner_draft(title="Owner checklist", body="Follow the owner's checklist.\n", *, resources=True, purpose="Owner procedure."):
    value = {"schema_version": "fa-team-owner-skill-draft-v1", "title": title,
             "arguments": {"purpose": purpose, "body": body}}
    if resources:
        value["arguments"]["resources"] = [{"name": "notes.md", "media_type": "text/markdown", "text": OWNER_RESOURCE}]
    return value


def post_draft(c, body, identity="owner"):
    return c.client.post(PREFIX + "/drafts", headers=c.headers(identity), json=body)


def test_owner_draft_is_an_unapproved_skill_and_the_same_content_finds_the_same_draft(team_skills):
    from shared_context.m3_skills import ApprovedSkillSource

    c = team_skills
    created = post_draft(c, owner_draft())
    assert created.status_code == 200, created.text
    assert created.headers["cache-control"] == "no-store"
    entry = created.json()["entry"]
    assert created.json()["schema_version"] == "fa-team-owner-skill-draft-result-v1"
    assert entry["status"] == "awaiting_decision" and entry["owner"] == "team-owner"
    assert entry["title"] == "Owner checklist" and entry["native_source"]["kind"] == "team-owner"
    assert entry["native_source"]["owner_oid"] == HUMAN_OID
    artifact = entry["artifact_id"]

    again = post_draft(c, owner_draft())
    assert again.status_code == 200, again.text
    assert again.json()["entry"] == entry
    listed = c.client.get(PREFIX + "/reviews", headers=c.headers()).json()["entries"]
    assert [item["artifact_id"] for item in listed] == [artifact]

    version_id = entry["latest_version"]["version_id"]
    content = c.client.get(PREFIX + f"/skills/{artifact}/versions/{version_id}/content", headers=c.headers())
    assert content.content == b"Follow the owner's checklist.\n"
    resource = c.client.get(PREFIX + f"/skills/{artifact}/versions/{version_id}/resources/notes.md", headers=c.headers())
    assert resource.status_code == 200 and resource.json()["text"] == OWNER_RESOURCE

    record = c.client.get(PREFIX + f"/skills/{artifact}", headers=c.headers())
    approved = c.client.post(PREFIX + f"/skills/{artifact}/decisions", headers={
        **c.headers(), "If-Match": record.headers["etag"]}, json=decision_body(entry["latest_version"]))
    assert approved.status_code == 200, approved.text
    catalog = ApprovedSkillSource(c.service).catalog(A, A.team, (artifact,), schema_version="fa-approved-skill-catalog-v2")
    assert [(item.title, item.description) for item in catalog.entries] == [("Owner checklist", "Owner procedure.")]
    assert catalog.entries[0].native_submit_sha256 == entry["native_source"]["submit_sha256"]
    read = c.service.skill_resource(A, A.team, artifact, version_id, "notes.md")
    assert read.text == OWNER_RESOURCE

    # After a decision the same content still finds that draft; changed content is a separate new draft.
    assert post_draft(c, owner_draft()).json()["entry"]["status"] == "effective"
    changed = post_draft(c, owner_draft(body="Follow the revised checklist.\n"))
    assert changed.status_code == 200 and changed.json()["entry"]["artifact_id"] != artifact
    assert changed.json()["entry"]["status"] == "awaiting_decision"


def test_owner_draft_needs_the_owner_with_approve_and_keeps_agent_and_owner_paths_apart(team_skills):
    from shared_context.m3_auth import HumanApprover
    from shared_context.m3_models import NativeArtifactSource, OwnerArtifactSource

    c = team_skills
    for identity in ("read_only", "agent", "other_human"):
        denied = post_draft(c, owner_draft(), identity)
        assert denied.status_code == 403 and denied.json()["error"]["code"] == "TEAM_SKILLS_OWNER_ONLY", denied.text
    assert c.client.get(PREFIX + "/reviews", headers=c.headers()).json()["entries"] == []

    entry = post_draft(c, owner_draft(resources=False)).json()["entry"]
    record = c.service.get(HumanApprover(HUMAN_OID, A.team), A.team, entry["artifact_id"])
    data = b"Agent replacement.\n"
    with pytest.raises(Exception) as agent_write:
        c.service.upload(A, A.team, entry["artifact_id"], "f" * 32, record.etag, VersionInput(
            filename="SKILL.md", media_type="text/markdown", sha256=hashlib.sha256(data).hexdigest()), data,
            native_source=OwnerArtifactSource.model_validate(entry["native_source"]))
    assert agent_write.value.body["error"]["code"] in {"M3_NOT_OWNER", "M3_NATIVE_SOURCE_DENIED"}

    human = HumanApprover(HUMAN_OID, A.team)
    agent_source = NativeArtifactSource(tenant_id=TENANT, team_id=A.team, agent_id="agent-a", agent_oid=A.oid,
                                        conversation_id="c1", request_id="r1", submit_sha256="0" * 64)
    with pytest.raises(Exception) as owner_as_agent:
        c.service.create_native_skill(human, A.team, "owner-as-agent", title="x", native_source=agent_source)
    assert owner_as_agent.value.body["error"]["code"] == "M3_NATIVE_SOURCE_DENIED"
    owner_source = OwnerArtifactSource(kind="team-owner", tenant_id=TENANT, team_id=A.team, owner_oid=HUMAN_OID,
                                       submit_sha256="0" * 64)
    with pytest.raises(Exception) as agent_as_owner:
        c.service.create_native_skill(A, A.team, "agent-as-owner", title="x", native_source=owner_source)
    assert agent_as_owner.value.body["error"]["code"] == "M3_NATIVE_SOURCE_DENIED"


@pytest.mark.parametrize("body", [
    owner_draft(title="two\nlines"),
    owner_draft(title=" padded"),
    owner_draft(title="x" * 65),
    owner_draft(body="x" * 8193),
    owner_draft(purpose=""),
    {**owner_draft(), "arguments": {**owner_draft()["arguments"], "resources": [
        {"name": f"r{index}.md", "media_type": "text/markdown", "text": "x"} for index in range(5)]}},
    {**owner_draft(), "arguments": {**owner_draft()["arguments"], "resources": [
        {"name": "same.md", "media_type": "text/markdown", "text": "a"},
        {"name": "same.md", "media_type": "text/markdown", "text": "b"}]}},
    {**owner_draft(), "arguments": {**owner_draft()["arguments"], "resources": [
        {"name": "../escape.md", "media_type": "text/markdown", "text": "a"}]}},
    {**owner_draft(), "secret": "never"},
])
def test_owner_draft_refuses_invalid_input_before_any_write(team_skills, body):
    c = team_skills
    refused = post_draft(c, body)
    assert refused.status_code in {413, 422}, refused.text
    assert c.client.get(PREFIX + "/reviews", headers=c.headers()).json()["entries"] == []


def test_owner_draft_after_a_lost_upload_reply_finishes_the_same_version(team_skills):
    c = team_skills
    original = c.service.store.put

    def lost_reply(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("reply lost after the blob was written")

    with patch.object(c.service.store, "put", side_effect=lost_reply):
        unknown = post_draft(c, owner_draft(resources=False))
    assert unknown.status_code == 503 and unknown.json()["error"]["code"] == "SKILL_DRAFT_OUTCOME_UNKNOWN"
    pending = c.client.get(PREFIX + "/reviews", headers=c.headers()).json()["entries"]
    assert [item["status"] for item in pending] == ["upload_pending"]

    finished = post_draft(c, owner_draft(resources=False))
    assert finished.status_code == 200, finished.text
    entry = finished.json()["entry"]
    assert entry["status"] == "awaiting_decision" and entry["latest_version"]["number"] == 1
    assert entry["artifact_id"] == pending[0]["artifact_id"]

@pytest.mark.parametrize("lost", ["skill", "resource"])
def test_owner_draft_whose_bytes_never_landed_is_finished_by_sending_the_same_content(team_skills, lost):
    c = team_skills
    original, calls = c.service.store.put, []

    def failed_before_writing(team, artifact, version, data, sha256):
        calls.append(data)
        is_resource = data == OWNER_RESOURCE.encode("utf-8")
        if (lost == "resource") == is_resource:
            raise OSError("connection reset before the blob was written")
        return original(team, artifact, version, data, sha256)

    with patch.object(c.service.store, "put", side_effect=failed_before_writing):
        unknown = post_draft(c, owner_draft())
    assert unknown.status_code == 503 and unknown.json()["error"]["code"] == "SKILL_DRAFT_OUTCOME_UNKNOWN"

    finished = post_draft(c, owner_draft())
    assert finished.status_code == 200, finished.text
    entry = finished.json()["entry"]
    assert entry["status"] == "awaiting_decision" and entry["latest_version"]["number"] == 1
    version_id = entry["latest_version"]["version_id"]
    content = c.client.get(PREFIX + f"/skills/{entry['artifact_id']}/versions/{version_id}/content", headers=c.headers())
    assert content.content == b"Follow the owner's checklist.\n"
    resource = c.client.get(PREFIX + f"/skills/{entry['artifact_id']}/versions/{version_id}/resources/notes.md",
                            headers=c.headers())
    assert resource.json()["text"] == OWNER_RESOURCE
    assert [item["artifact_id"] for item in c.client.get(PREFIX + "/reviews", headers=c.headers()).json()["entries"]] == [
        entry["artifact_id"]]
