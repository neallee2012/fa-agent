"""從 test_sop_skill_copy.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import hashlib
from conftest import A
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, SkillPurpose, VersionInput
from shared_context.models import CreateWork
from shared_context.source_materials import digest


SOP = "copy-source-sop"


LEGACY_WORK = "copy-source-work"


SOURCE_VERSION = "c" * 32


BODY = "APPROVED_TEAM_SOP_CANARY\r\n核對原始回條，不批准操作。\t\n".encode("utf-8")


PURPOSE = "  用於核對已保存回條；僅為草稿，不是批准。  "


def procedure(body):
    return VersionInput(filename="procedure.md", media_type="text/markdown", sha256=hashlib.sha256(body).hexdigest())


def approved_sop(h, signing_key, jwk_client, *, body=BODY, version=SOURCE_VERSION, approve=True, kind="sop"):
    """Returns (ArtifactService, agent, configured human, source version) built in process."""
    artifacts = h.artifacts
    human = HumanApprover(artifacts.approval.approver_oid, artifacts.approval.team)
    artifacts.context.create(A, A.team, LEGACY_WORK, CreateWork(goal="Synthetic existing Team work for an approved procedure."))
    created = artifacts.create(A, A.team, SOP, CreateArtifact(
        kind=kind, title="Approved shared Team procedure", source_work=LEGACY_WORK,
    ))
    uploaded = artifacts.upload(A, A.team, SOP, version, created.etag, procedure(body), body)
    if approve:
        artifacts.decide(human, A.team, SOP, uploaded.etag, ApprovalInput(
            decision_id=version, version_id=version, sha256=hashlib.sha256(body).hexdigest(), decision="approve",
        ))
    return artifacts, A, human, artifacts.version(A, A.team, SOP, version).model_dump(mode="json")


def copy_scope(version):
    return {
        "schema_version": "fa-team-sop-skill-copy-v1",
        "authorization": "one-approved-sop-byte-copy-per-work",
        "visibility": "team", "source_visibility": "team", "team_id": A.team,
        "source_artifact_id": SOP, "source_version_id": version["version_id"],
        "source_sha256": version["sha256"], "source_approval_sha256": digest(version["approval"]),
        "purpose": SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE).model_dump(),
    }
