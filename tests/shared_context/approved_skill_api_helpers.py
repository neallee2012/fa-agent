"""從 test_approved_skill_api.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import hashlib
import copy
from types import MappingProxyType, SimpleNamespace
from conftest import A
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, CreateArtifact, VersionInput
from work_policy_lifecycle_helpers import enabled


BODY = "APPROVED_SKILL_BODY_CANARY: report only the verified receipt; never grant operations.\n"


def add_skill(h, name="receipt-guide", *, text=BODY, kind="skill", choice="approve", version="1" * 32):
    record = h.artifacts.create(A, A.team, name, CreateArtifact(
        kind=kind, title="Receipt verification guide", source_work="legacy-source"))
    raw = text.encode("utf-8")
    record = h.artifacts.upload(A, A.team, name, version, record.etag, VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(raw).hexdigest()), raw)
    if choice is not None:
        h.artifacts.decide(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, name, record.etag,
            ApprovalInput(decision_id=version, version_id=version, sha256=hashlib.sha256(raw).hexdigest(), decision=choice))


def approve_skill_version(h, name, text, version="2" * 32):
    record = h.artifacts.get(A, A.team, name)
    raw = text.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    uploaded = h.artifacts.upload(
        A, A.team, name, version, record.etag,
        VersionInput(filename="guide.md", media_type="text/markdown", sha256=sha), raw)
    h.artifacts.decide(
        HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team, name, uploaded.etag,
        ApprovalInput(decision_id=version, version_id=version, sha256=sha, decision="approve"))


def configured(assistant, selected=("receipt-guide",)):
    assistant.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate(profile.model_copy(update={
            "prompt_profile": "fa-instruction-skills-v1", "skill_artifacts": selected}).model_dump())
        for name, profile in assistant.runtime.profiles.items()
    })
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    return h, control, human


def change_artifact(h, change, name="receipt-guide"):
    record = h.artifacts.get(A, A.team, name)
    payload = copy.deepcopy(record.payload)
    change(payload)
    partition = "m3-" + h.artifacts.context.auth.tenant_id + "-" + A.team
    h.artifacts.repository.replace(partition, "artifact:" + name, payload, record.etag)
