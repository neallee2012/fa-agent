"""A saved artifact document keeps its exact acceptance rules, messages and check order."""

import pytest
from pydantic import ValidationError

from shared_context.m3_models import MAX_ARTIFACT_VERSIONS, ArtifactDocument, skill_metadata_sha256

TEAM, ARTIFACT, OWNER = "team-one", "guide", "agent-a"
OWNER_OID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_OID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
TENANT_OID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
OTHER_TENANT_OID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
APPROVER_OID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
AT = "2026-11-01T00:00:00+00:00"
PURPOSE = {"schema_version": "fa-skill-purpose-v1", "description": "Follow the approved procedure."}
NATIVE = {
    "kind": "native-turn", "tenant_id": TENANT_OID, "team_id": TEAM, "agent_id": "native-agent",
    "agent_oid": OWNER_OID, "conversation_id": "original-cid", "request_id": "original-rid",
    "submit_sha256": "3" * 64,
}
TEAM_OWNER = {
    "kind": "team-owner", "tenant_id": TENANT_OID, "team_id": TEAM, "owner_oid": OWNER_OID,
    "submit_sha256": "3" * 64,
}
LEGACY_FIELDS = {"source_work": "source-work", "source_work_revision": 1}

MIXED_SOURCE = "exactly one legacy work or native turn source is required"
SOURCE_OWNER = "the native source must belong to the artifact's Team and owner"
OWNER_NAME = "only a Team owner source uses the Team owner author name"
MIXED_VERSION = "native and legacy artifact provenance cannot be mixed"
VERSION_TEAM = "a native artifact version cannot cross Teams"
VERSION_AUTHOR = "the native artifact source must retain the original author"
VERSION_PURPOSE = "Only skill versions may carry an approved purpose"
VERSION_DIGEST = "Skill version metadata differs from its immutable digest"
PENDING_PURPOSE = "Only skill versions may propose a purpose"
PENDING_TEAM = "a native pending version cannot cross Teams"
MIXED_PENDING = "native and legacy pending provenance cannot be mixed"
SEQUENCE = "version history must be unique and sequential"
INVALID_PENDING = "invalid pending version"
PROVENANCE = "version provenance must match the artifact owner"
DELIVERABLE = "ordinary deliverables do not activate policies"
SKILLS_ONLY = "Only skills support explicit deactivation"
DEACTIVATION_VERSION = "Deactivation must retain an exact approved skill version"
DEACTIVATION_ORDER = "Deactivation history must be unique and ordered"
DECISION_REUSE = "decision IDs cannot be reused"
REACTIVATED = "A deactivated version cannot silently become effective again"
EFFECTIVE = "effective version must have an exact human approval"


def identifier(number):
    return f"{number:032x}"


def digest(number):
    return f"{number:064x}"


def version(number=1, decision=None, *, team=TEAM, artifact=ARTIFACT, **changes):
    """One version sealed the way the service seals it; `None` drops a field."""
    value = {
        "filename": "guide.md", "media_type": "text/markdown", "sha256": digest(number),
        "version_id": identifier(number), "number": number, "created_by_oid": OWNER_OID, "created_at": AT,
        "skill_purpose": PURPOSE, **LEGACY_FIELDS,
        "blob": {"native_version": "blob-v1", "etag": '"receipt"', "sha256": digest(number), "bytes": 10},
        **changes,
    }
    value = {name: item for name, item in value.items() if item is not None}
    metadata = skill_metadata_sha256(team, artifact, value)
    if metadata is not None:
        value["metadata_sha256"] = metadata
    if decision is not None:
        value["approval"] = {
            "decision_id": identifier(100 + number), "version_id": value["version_id"], "sha256": value["sha256"],
            "decision": decision, "approver_oid": APPROVER_OID, "decided_at": AT,
            **({} if metadata is None else {"metadata_sha256": metadata}),
        }
    return value


def native_version(number=1, decision=None, source=NATIVE, **changes):
    return version(number, decision, native_source=source, source_work=None, source_work_revision=None, **changes)


def pending(number=9, **changes):
    value = {
        "filename": "guide.md", "media_type": "text/markdown", "sha256": digest(number),
        "version_id": identifier(number), "bytes": 10, "created_by_oid": OWNER_OID, "created_at": AT,
        **LEGACY_FIELDS, **changes,
    }
    return {name: item for name, item in value.items() if item is not None}


def native_pending(number=9, source=NATIVE, **changes):
    return pending(number, native_source=source, source_work=None, source_work_revision=None, **changes)


def deactivation(item, *, decision_id=200, applied_revision=2, **changes):
    return {
        "schema_version": "fa-skill-deactivation-v1", "decision_id": identifier(decision_id),
        "version_id": item["version_id"], "sha256": item["sha256"], "metadata_sha256": item["metadata_sha256"],
        "deactivated_by_oid": APPROVER_OID, "deactivated_at": AT, "applied_revision": applied_revision, **changes,
    }


def document(**changes):
    value = {
        "kind": "skill", "title": "Guide", "source_work": "source-work", "artifact_id": ARTIFACT, "team": TEAM,
        "owner": OWNER, "owner_oid": OWNER_OID, "revision": 5, **changes,
    }
    return {name: item for name, item in value.items() if item is not None}


def native_document(source=NATIVE, **changes):
    return document(source_work=None, native_source=source, **changes)


def owner_document(**changes):
    return native_document(TEAM_OWNER, owner="team-owner", **changes)


def plain(number=1, decision=None):
    return version(number, decision, skill_purpose=None)


def full_versions():
    return [plain(number) for number in range(1, MAX_ARTIFACT_VERSIONS + 1)]


FIRST, SECOND = version(1, "approve"), version(2, "approve")
BROKEN_DIGEST = {**version(), "metadata_sha256": "f" * 64}
OTHER_TEAM_SOURCE = {**NATIVE, "team_id": "team-two"}
OTHER_AUTHOR_SOURCE = {**NATIVE, "agent_id": "other-agent"}


@pytest.mark.parametrize("value", [
    pytest.param(document(), id="empty legacy history"),
    pytest.param(native_document(), id="empty native history"),
    pytest.param(owner_document(), id="empty Team owner history"),
    pytest.param(document(kind="sop", versions=[plain(1, "approve")], effective_version_id=identifier(1)),
                 id="approved sop"),
    pytest.param(document(kind="deliverable", versions=[plain()]), id="plain deliverable"),
    pytest.param(document(versions=[FIRST, SECOND], effective_version_id=identifier(2), revision=1,
                          deactivations=[deactivation(FIRST, applied_revision=1)]),
                 id="deactivation at the first and last revision"),
    pytest.param(native_document(versions=[native_version(1, "approve"), native_version(2)],
                                 pending=native_pending(), effective_version_id=identifier(1)),
                 id="native history with pending"),
    pytest.param(owner_document(versions=[native_version(source=TEAM_OWNER)], pending=native_pending(source=TEAM_OWNER)),
                 id="Team owner history with pending"),
    pytest.param(document(versions=[version()], pending=pending(skill_purpose=PURPOSE)), id="skill pending purpose"),
    pytest.param(document(kind="sop", versions=[plain(number) for number in range(1, MAX_ARTIFACT_VERSIONS)],
                          pending=pending(99)), id="pending fits the last open slot"),
    pytest.param(document(kind="sop", versions=full_versions()), id="full history"),
    pytest.param(document(versions=[FIRST, SECOND], deactivations=[
        deactivation(FIRST, applied_revision=2), deactivation(SECOND, decision_id=201, applied_revision=3),
    ]), id="two ordered deactivations"),
])
def test_saved_documents_that_keep_every_rule_still_load_unchanged(value):
    loaded = ArtifactDocument.model_validate(value)
    assert ArtifactDocument.model_validate(loaded.model_dump(mode="json")) == loaded


def test_the_document_schema_has_only_its_saved_fields():
    assert list(ArtifactDocument.model_fields) == [
        "kind", "title", "source_work", "schema_version", "artifact_id", "team", "owner", "owner_oid",
        "native_source", "revision", "versions", "pending", "effective_version_id", "deactivations",
    ]
    assert list(ArtifactDocument.__private_attributes__) == []


def rejected(message, value):
    with pytest.raises(ValidationError) as denied:
        ArtifactDocument.model_validate(value)
    assert [item["msg"] for item in denied.value.errors()] == ["Value error, " + message]


@pytest.mark.parametrize("message,value", [
    pytest.param(MIXED_SOURCE, document(source_work=None), id="no source"),
    pytest.param(MIXED_SOURCE, document(native_source=NATIVE), id="both sources"),
    pytest.param(SOURCE_OWNER, native_document(OTHER_TEAM_SOURCE), id="source from another Team"),
    pytest.param(SOURCE_OWNER, native_document({**NATIVE, "agent_oid": OTHER_OID}), id="source from another owner"),
    pytest.param(OWNER_NAME, native_document(TEAM_OWNER), id="owner source without the owner name"),
    pytest.param(OWNER_NAME, native_document(owner="team-owner"), id="agent source with the owner name"),
    pytest.param(OWNER_NAME, document(owner="team-owner"), id="legacy source with the owner name"),
    pytest.param(MIXED_VERSION, document(versions=[native_version()]), id="native version in a legacy history"),
    pytest.param(MIXED_VERSION, native_document(versions=[version()]), id="legacy version in a native history"),
    pytest.param(MIXED_VERSION, document(versions=[native_version(source=OTHER_TEAM_SOURCE)]),
                 id="native version from another Team in a legacy history"),
    pytest.param(VERSION_TEAM, native_document(versions=[native_version(source=OTHER_TEAM_SOURCE)]),
                 id="version from another Team"),
    pytest.param(VERSION_AUTHOR, native_document(versions=[native_version(source=OTHER_AUTHOR_SOURCE)]),
                 id="version from another agent"),
    pytest.param(VERSION_AUTHOR, native_document(versions=[native_version(
        source={**NATIVE, "tenant_id": OTHER_TENANT_OID})]), id="version from another tenant"),
    pytest.param(VERSION_AUTHOR, native_document(versions=[native_version(source=TEAM_OWNER)]),
                 id="version from the Team owner in an agent history"),
    pytest.param(VERSION_PURPOSE, document(kind="sop", versions=[version()]), id="sop version purpose"),
    pytest.param(VERSION_PURPOSE, document(kind="deliverable", versions=[version()]), id="deliverable version purpose"),
    pytest.param(VERSION_DIGEST, document(versions=[BROKEN_DIGEST]), id="changed digest"),
    pytest.param(VERSION_DIGEST, document(versions=[version(team="team-two")]), id="digest from another Team"),
    pytest.param(VERSION_DIGEST, document(versions=[version(artifact="other-guide")]), id="digest from another artifact"),
    pytest.param(PENDING_PURPOSE, document(kind="sop", pending=pending(skill_purpose=PURPOSE)), id="sop pending purpose"),
    pytest.param(PENDING_TEAM, native_document(pending=native_pending(source=OTHER_TEAM_SOURCE)),
                 id="pending from another Team"),
    pytest.param(MIXED_PENDING, document(pending=native_pending()), id="native pending in a legacy history"),
    pytest.param(MIXED_PENDING, native_document(pending=pending()), id="legacy pending in a native history"),
    pytest.param(SEQUENCE, document(versions=[version(), version(2, version_id=identifier(1))]), id="repeated version"),
    pytest.param(SEQUENCE, document(versions=[version(2)]), id="history starts late"),
    pytest.param(SEQUENCE, document(versions=[version(), version(3)]), id="history has a gap"),
    pytest.param(INVALID_PENDING, document(versions=[version()], pending=pending(1)), id="pending repeats a version"),
    pytest.param(INVALID_PENDING, document(kind="sop", versions=full_versions(), pending=pending(99)),
                 id="pending beyond the version limit"),
    pytest.param(PROVENANCE, document(versions=[version(created_by_oid=OTHER_OID)]), id="version by another owner"),
    pytest.param(PROVENANCE, document(pending=pending(created_by_oid=OTHER_OID)), id="pending by another owner"),
    pytest.param(DELIVERABLE, document(kind="deliverable", effective_version_id=identifier(1)), id="effective deliverable"),
    pytest.param(DELIVERABLE, document(kind="deliverable", versions=[plain(1, "reject")]), id="decided deliverable"),
    pytest.param(SKILLS_ONLY, document(kind="sop", deactivations=[deactivation(FIRST)]), id="sop deactivation"),
    pytest.param(DEACTIVATION_VERSION, document(deactivations=[deactivation(FIRST)]), id="unknown version"),
    pytest.param(DEACTIVATION_VERSION, document(versions=[version()], deactivations=[deactivation(FIRST)]),
                 id="undecided version"),
    pytest.param(DEACTIVATION_VERSION, document(versions=[version(1, "reject")], deactivations=[deactivation(FIRST)]),
                 id="rejected version"),
    pytest.param(DEACTIVATION_VERSION, document(versions=[FIRST], deactivations=[deactivation(FIRST, sha256="f" * 64)]),
                 id="other content digest"),
    pytest.param(DEACTIVATION_VERSION, document(versions=[FIRST], deactivations=[
        deactivation(FIRST, metadata_sha256="f" * 64)]), id="other metadata digest"),
    pytest.param(DEACTIVATION_ORDER, document(versions=[FIRST], deactivations=[
        deactivation(FIRST), deactivation(FIRST, decision_id=201, applied_revision=3)]), id="same version twice"),
    pytest.param(DEACTIVATION_ORDER, document(versions=[FIRST, SECOND], deactivations=[
        deactivation(FIRST, applied_revision=3), deactivation(SECOND, decision_id=201, applied_revision=3)]),
        id="repeated revision"),
    pytest.param(DEACTIVATION_ORDER, document(versions=[FIRST, SECOND], deactivations=[
        deactivation(FIRST, applied_revision=3), deactivation(SECOND, decision_id=201, applied_revision=2)]),
        id="revision moved backwards"),
    pytest.param(DEACTIVATION_ORDER, document(versions=[FIRST], deactivations=[deactivation(FIRST, applied_revision=6)]),
                 id="revision after the document"),
    pytest.param(DECISION_REUSE, document(versions=[FIRST, {
        **SECOND, "approval": {**SECOND["approval"], "decision_id": FIRST["approval"]["decision_id"]}}]),
        id="two approvals share a decision"),
    pytest.param(DECISION_REUSE, document(versions=[FIRST], deactivations=[
        deactivation(FIRST, decision_id=101)]), id="deactivation reuses an approval decision"),
    pytest.param(DECISION_REUSE, document(versions=[FIRST, SECOND], deactivations=[
        deactivation(FIRST), deactivation(SECOND, applied_revision=3)]), id="two deactivations share a decision"),
    pytest.param(REACTIVATED, document(versions=[FIRST], effective_version_id=identifier(1),
                                       deactivations=[deactivation(FIRST)]), id="deactivated version made effective"),
    pytest.param(EFFECTIVE, document(effective_version_id=identifier(1)), id="unknown effective version"),
    pytest.param(EFFECTIVE, document(versions=[version()], effective_version_id=identifier(1)), id="undecided effective"),
    pytest.param(EFFECTIVE, document(versions=[version(1, "reject")], effective_version_id=identifier(1)),
                 id="rejected effective"),
])
def test_each_rule_refuses_only_its_own_violation_with_its_exact_message(message, value):
    rejected(message, value)


@pytest.mark.parametrize("message,value", [
    pytest.param(MIXED_SOURCE, document(native_source=OTHER_TEAM_SOURCE), id="source pair before source owner"),
    pytest.param(SOURCE_OWNER, native_document(OTHER_TEAM_SOURCE, owner="team-owner"),
                 id="source owner before owner name"),
    pytest.param(OWNER_NAME, document(owner="team-owner", versions=[native_version()]),
                 id="owner name before versions"),
    pytest.param(VERSION_TEAM, native_document(versions=[
        native_version(source={**OTHER_TEAM_SOURCE, "agent_id": "other-agent"})]), id="version Team before author"),
    pytest.param(VERSION_AUTHOR, native_document(kind="sop", versions=[native_version(source=OTHER_AUTHOR_SOURCE)]),
                 id="version author before purpose"),
    pytest.param(VERSION_PURPOSE, document(kind="sop", versions=[BROKEN_DIGEST]), id="version purpose before digest"),
    pytest.param(VERSION_TEAM, native_document(versions=[native_version(source=OTHER_TEAM_SOURCE), version(2)]),
                 id="first version before a later version"),
    pytest.param(VERSION_DIGEST, document(versions=[BROKEN_DIGEST], pending=native_pending()),
                 id="versions before pending"),
    pytest.param(PENDING_PURPOSE, native_document(kind="sop", pending=native_pending(
        source=OTHER_TEAM_SOURCE, skill_purpose=PURPOSE)), id="pending purpose before Team"),
    pytest.param(PENDING_TEAM, document(pending=native_pending(source=OTHER_TEAM_SOURCE)),
                 id="pending Team before mixed pending"),
    pytest.param(MIXED_PENDING, document(versions=[version(2)], pending=native_pending()),
                 id="mixed pending before sequence"),
    pytest.param(SEQUENCE, document(versions=[version(2)], pending=pending(2)), id="sequence before pending slot"),
    pytest.param(INVALID_PENDING, document(versions=[version(created_by_oid=OTHER_OID)], pending=pending(1)),
                 id="pending slot before provenance"),
    pytest.param(PROVENANCE, document(kind="deliverable", versions=[plain(1, "approve") | {"created_by_oid": OTHER_OID}]),
                 id="provenance before deliverable"),
    pytest.param(DELIVERABLE, document(kind="deliverable", effective_version_id=identifier(1),
                                       deactivations=[deactivation(FIRST)]), id="deliverable before skills only"),
    pytest.param(SKILLS_ONLY, document(kind="sop", deactivations=[deactivation(FIRST, applied_revision=6)]),
                 id="skills only before deactivation version"),
    pytest.param(DEACTIVATION_VERSION, document(deactivations=[deactivation(FIRST, applied_revision=6)]),
                 id="deactivation version before order"),
    pytest.param(DEACTIVATION_ORDER, document(versions=[FIRST, SECOND], deactivations=[
        deactivation(FIRST, applied_revision=3), deactivation(SECOND, applied_revision=3)]),
        id="deactivation order before decision reuse"),
    pytest.param(DEACTIVATION_ORDER, document(versions=[FIRST], deactivations=[
        deactivation(FIRST, applied_revision=6), deactivation(SECOND, decision_id=201)]),
        id="first deactivation before a later one"),
    pytest.param(DECISION_REUSE, document(versions=[FIRST], effective_version_id=identifier(1),
                                          deactivations=[deactivation(FIRST, decision_id=101)]),
                 id="decision reuse before reactivation"),
    pytest.param(DECISION_REUSE, document(versions=[FIRST, version(2, "reject") | {"approval": {
        **version(2, "reject")["approval"], "decision_id": FIRST["approval"]["decision_id"]}}],
        effective_version_id=identifier(2)), id="decision reuse before effective approval"),
])
def test_the_earlier_rule_reports_when_a_document_breaks_two(message, value):
    rejected(message, value)
