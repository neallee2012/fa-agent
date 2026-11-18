"""從 test_instruction_attachments.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import hashlib
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_attachments import TextAttachment
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_wire import Command, ExactSourceRef, SourcePrepare
from shared_context.source_materials import SourceWithdrawalInput
from work_policy_lifecycle_helpers import enabled


BODY = "  ATTACHED_SOURCE_CANARY\r\n原始\u3000文字與數值42。\n"


def configured_attachments(h, *, active=True, successor=None, expires=False, necessary=(), visibility="team"):
    h.policy = h.policy.model_copy(update={"human_text_enabled": True, "text_policy_version": "human-text-policy-v1"})
    h.mutate(runtime_key("policy", h.binding.binding_id), human_text_enabled=True, text_policy_version="human-text-policy-v1")
    h.runtime.profiles = MappingProxyType({
        key: AssistantProfile.model_validate({**value.model_dump(), "prompt_profile": "fa-instruction-v1",
            **({"text_attachment_profile": "fa-source-text-attachments-v1"} if active else {})})
        for key, value in h.runtime.profiles.items()
    })
    h.sources._clock = lambda: h.time
    attachment = add_source(h, BODY, expires_at=h.time + timedelta(seconds=1) if expires else None, necessary=necessary)
    if successor is not None:
        add_source(h, successor, version=2)
    if visibility != "team":
        h.policy = h.policy.model_copy(update={"source_visibility": visibility})
        h.mutate(runtime_key("policy", h.binding.binding_id), source_visibility=visibility)
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    return h, control, human, attachment


def add_source(h, text, version=1, expires_at=None, necessary=()):
    raw = text.encode("utf-8")
    command = SourcePrepare(
        request_id=f"attachment-source-{version}", version=version, original=raw, source_schema="fa-human-text-v1",
        retention_consent_sha256=hashlib.sha256(raw).hexdigest(), expires_at=expires_at, necessary=necessary,
    )
    response = h.post("/sources/dependency/prepare", command)
    assert response.status_code == 200, response.text
    response = h.post("/sources/dependency/commit", Command(request_id=command.request_id))
    assert response.status_code == 200, response.text
    return TextAttachment(name="reference.txt", source=ExactSourceRef(
        source_id="dependency", version=version, sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw),
    ))


def withdraw(h, control, human, attachment):
    context = h.runtime._source_context(control.policy.template, control.policy.legacy_template_policy, human)
    reference = h.runtime._reference(attachment.source, control.policy.template, control.policy.legacy_template_policy, human)
    h.sources.withdraw_source(human, context, SourceWithdrawalInput(
        withdrawal_id="withdraw-attachment", reference=reference, expected_access_revision=1,
        reason="The original human withdrew this attachment.",
    ))
