"""Native provenance on the existing M3 service; no new artifact platform or fake work."""

import hashlib
import copy

import pytest
from pydantic import ValidationError

from conftest import A, B, TENANT
from shared_context.errors import Problem
from shared_context.m3_models import ApprovalInput, ArtifactDocument, CreateArtifact, SkillPurpose, VersionInput
from shared_context.m3_skills import ApprovedSkillSource
from test_m3_artifacts import HUMAN, V1, V2, create, m3, upload


def native_source():
    from shared_context.m3_models import NativeArtifactSource

    return NativeArtifactSource(
        kind="native-turn", tenant_id=TENANT, team_id=A.team, agent_id="native-agent", agent_oid=A.oid,
        conversation_id="native-original-cid", request_id="native-original-rid", submit_sha256="3" * 64)


def test_native_draft_uses_original_m3_list_approval_and_approved_skill_reader_without_a_legacy_work(m3, monkeypatch):
    service, _repository, _store = m3
    origin = native_source()
    body = b"Use the original receipt before updating the approved procedure.\n"
    purpose = SkillPurpose(schema_version="fa-skill-purpose-v1", description="Follow the supplied original procedure.")

    def no_legacy(*_args, **_kwargs):
        pytest.fail("native source may not be presented as a legacy Context work")

    monkeypatch.setattr(service.context, "get", no_legacy)
    created = service.create_native_skill(A, A.team, "native-draft",
                                          title="Unapproved native procedure draft", native_source=origin)
    metadata = VersionInput(filename="skill-draft.md", media_type="text/markdown",
                            sha256=hashlib.sha256(body).hexdigest(), skill_purpose=purpose)
    saved = service.upload(A, A.team, "native-draft", V1, created.etag, metadata, body, native_source=origin)
    document = ArtifactDocument.model_validate(saved.payload)
    version = document.versions[0]
    assert document.source_work is None and document.native_source == origin
    assert version.source_work is None and version.source_work_revision is None and version.native_source == origin
    assert "source_work" not in saved.payload and "source_work_revision" not in saved.payload["versions"][0]
    assert version.approval is None and document.effective_version_id is None
    listed = service.skill_reviews(HUMAN, A.team)
    assert listed.entries[0].status == "awaiting_decision" and listed.entries[0].native_source == origin
    assert service.download(HUMAN, A.team, "native-draft", V1, version.blob.etag)[1] == body
    approved = service.decide(HUMAN, A.team, "native-draft", saved.etag, ApprovalInput(
        decision_id="4" * 32, version_id=V1, sha256=version.sha256, metadata_sha256=version.metadata_sha256,
        decision="approve"))
    assert approved.payload["effective_version_id"] == V1
    source = ApprovedSkillSource(service)
    catalog = source.catalog(A, A.team, ("native-draft",), schema_version="fa-approved-skill-catalog-v2")
    assert catalog.entries[0].native_submit_sha256 == origin.submit_sha256
    assert source.read(A, A.team, ("native-draft",), catalog.entries[0]).text == body.decode()


def test_public_m3_create_and_upload_inputs_cannot_claim_a_native_source(m3):
    service, repository, store = m3
    origin = native_source()
    for payload in (
        {"kind": "skill", "title": "Missing source"},
        {"kind": "skill", "title": "Forged source", "native_source": origin.model_dump()},
        {"kind": "skill", "title": "Forged source", "source_work": "source-work",
         "native_source": origin.model_dump()},
    ):
        with pytest.raises(ValidationError):
            CreateArtifact.model_validate(payload)
    with pytest.raises(ValidationError):
        VersionInput.model_validate({"filename": "draft.md", "media_type": "text/markdown", "sha256": "a" * 64,
                                     "native_source": origin.model_dump()})
    created = service.create_native_skill(A, A.team, "native-only", title="Native only", native_source=origin)
    before = copy.deepcopy(repository.data)
    content = b"Original draft"
    metadata = VersionInput(filename="draft.md", media_type="text/markdown", sha256=hashlib.sha256(content).hexdigest())
    with pytest.raises(Problem) as generic:
        service.upload(A, A.team, "native-only", V1, created.etag, metadata, content)
    assert generic.value.body["error"]["code"] == "M3_NATIVE_SOURCE_DENIED"
    assert repository.data == before
    with pytest.raises(Problem):
        service.upload(B, A.team, "native-only", V1, created.etag, metadata, content, native_source=origin)
    assert repository.data == before


def test_native_metadata_stays_immutable_and_legacy_payloads_keep_their_original_shape(m3):
    service, _, _ = m3
    create(service, "skill")
    legacy = upload(service)
    legacy_copy = copy.deepcopy(legacy.payload)
    origin = native_source()
    value = service.create_native_skill(A, A.team, "native-purpose", title="Native", native_source=origin)
    body = b"Follow the exact provided procedure."
    metadata = VersionInput(
        filename="draft.md", media_type="text/markdown", sha256=hashlib.sha256(body).hexdigest(),
        skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description="Original purpose"))
    saved = service.upload(A, A.team, "native-purpose", V1, value.etag, metadata, body, native_source=origin)
    assert service.get(A, A.team, "report").payload == legacy_copy
    assert "native_source" not in legacy_copy and "native_source" not in legacy_copy["versions"][0]
    mixed = copy.deepcopy(saved.payload)
    mixed["source_work"] = "source-work"
    with pytest.raises(ValidationError):
        ArtifactDocument.model_validate(mixed)
    forged = copy.deepcopy(saved.payload)
    forged["versions"][0]["native_source"]["submit_sha256"] = "f" * 64
    with pytest.raises(ValidationError):
        ArtifactDocument.model_validate(forged)
    with pytest.raises(Problem) as changed:
        service.upload(A, A.team, "native-purpose", V1, saved.etag, metadata, body,
                       native_source=origin.model_copy(update={"submit_sha256": "f" * 64}))
    assert changed.value.body["error"]["code"] == "M3_VERSION_CONFLICT"


def test_four_approved_native_drafts_fit_the_original_catalog_without_truncating_purpose(m3):
    service, _, _ = m3
    purpose = "Follow the original approved procedure, preserving exact input and receipt references. " * 2 + "Read."
    assert len(purpose.encode()) == 179
    names = tuple(f"{number:064x}" for number in range(1, 5))
    data = b"Read the original request and its retained receipt before acting."
    for number, name in enumerate(names, 1):
        source = native_source().model_copy(update={
            "agent_id": "11111111-1111-1111-1111-111111111111",
            "conversation_id": f"native-cid-{number:032x}", "request_id": f"native-rid-{number:032x}",
            "submit_sha256": f"{number:064x}"})
        created = service.create_native_skill(
            A, A.team, name, title="Unapproved skill draft from user procedure", native_source=source)
        metadata = VersionInput(filename="skill-draft.md", media_type="text/markdown",
            sha256=hashlib.sha256(data).hexdigest(),
            skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=purpose))
        saved = service.upload(A, A.team, name, V1, created.etag, metadata, data, native_source=source)
        version = saved.payload["versions"][0]
        service.decide(HUMAN, A.team, name, saved.etag, ApprovalInput(
            decision_id=f"{number:032x}", version_id=V1, sha256=version["sha256"],
            metadata_sha256=version["metadata_sha256"], decision="approve"))
    source = ApprovedSkillSource(service)
    catalog = source.catalog(A, A.team, names, schema_version="fa-approved-skill-catalog-v2")
    assert len(catalog.entries) == 4 and len(catalog.model_dump_json().encode("utf-8")) <= 4096
    assert all(entry.description == purpose for entry in catalog.entries)
    assert [entry.native_submit_sha256 for entry in catalog.entries] == [f"{number:064x}" for number in range(1, 5)]
    assert all("native_source" not in entry.model_dump() for entry in catalog.entries)
    assert [source.read(A, A.team, names, entry).text for entry in catalog.entries] == [data.decode()] * 4
    changed = catalog.entries[0].model_copy(update={"native_submit_sha256": "f" * 64})
    with pytest.raises(Problem) as refused:
        source.read(A, A.team, names, changed)
    assert refused.value.body["error"]["code"] == "SKILL_SOURCE_CHANGED"
