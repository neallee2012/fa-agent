"""User-provided procedure skill drafts: local signed API and SDK-I/O doubles."""

import hashlib
import json
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest

from conftest import A, B, C
from shared_context.behavior_assistant import AssistantProfile, key
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_skill_draft import (
    SKILL_DRAFT_PROFILE, SaveSkillDraftCommand, SkillDraftOutcome, SkillDraftReceipt, SkillDraftToolArgs,
)
from shared_context.behavior_skill_draft_prompt import (
    SKILL_DRAFT_CAPABILITIES, SKILL_DRAFT_PROMPT, assemble_skill_draft_prompt,
)
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy import WorkPolicyControl
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import signing_key, token, jwk_client
from test_sdk_compaction_product import open_turn, ordinary, settle
from test_work_cost_policy import standing

PURPOSE = "Procedure for preserving operator handoff steps."
BODY = "## Procedure\n\n1. Confirm the original request.\n2. Save only non-secret team guidance.\n"
OTHER = BODY + "3. Different content must not replace the original draft.\n"


OLD_PROMPTS = (
    (None, 552, "f2d312f2cab688a03d035adb971ead55e4fd5057c69bdc0eede97c5cb084799f", ()),
    ("fa-instruction-v1", 2094, "8e24b5291f5a46d3795e9c9b56c8e4dcde3af705deb23eaa08899ce0613963cb",
     ("read_context", "execute_operation")),
    ("fa-instruction-skills-v1", 2572, "14a9d2a605706db98e0e8cea877beec5ef9a86646dd21bc544049e0dabf4ab3e",
     ("read_context", "execute_operation", "read_skill")),
    ("fa-instruction-skill-purpose-v1", 3002, "65abc3b54e0cf9be7109f379c547a0a4cf66b18642b764a8f847a5d26bf67879",
     ("read_context", "execute_operation", "read_skill")),
    ("fa-instruction-user-turns-v1", 2822, "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372",
     ("read_context", "execute_operation", "read_user_turn")),
    ("fa-instruction-work-memory-v1", 2719, "1add3188623d9503311053721a13d931fa47c229e83b991f36b0abc7c97586f3",
     ("read_context", "execute_operation", "read_work_memory")),
    ("fa-instruction-sop-copy-v1", 2645, "8f6151198fefda9410c59070d311d5ddfb724d6bebc845165b8027aa66cefa13",
     ("read_context", "execute_operation", "copy_sop_to_skill_draft")),
    ("fa-instruction-skill-resources-v1", 3626, "1bc9b9e1375b4bda66bd9d418f3303c74aa2aa7dc6e3dc5f41f49207b117dcdd",
     ("read_context", "execute_operation", "read_skill", "read_skill_resource")),
    ("fa-instruction-user-files-v1", 3201, "9c8fd82691f5b40e5b01c5d90d15a807dec08828bdb9b2b3fb9ddf1f77965296",
     ("read_context", "execute_operation", "read_user_turn", "read_user_file")),
    ("fa-instruction-assistant-replies-v1", 3299, "bc7ec9b2f7ee9cb783ce21c51872e5a82e38841737b67caf37ac7c37ae50a983",
     ("read_context", "execute_operation", "read_user_turn", "read_assistant_reply")),
    ("fa-instruction-history-v1", 3678, "3d1c1e75d3eb95e36090c169195264cacee24d957e3e0e356b76f25db0d5648f",
     ("read_context", "execute_operation", "read_user_turn", "read_user_file", "read_assistant_reply")),
)


def configured_draft(h):
    h.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({
            **profile.model_dump(), "prompt_profile": SKILL_DRAFT_PROFILE,
        }) for name, profile in h.runtime.profiles.items()
    })
    _, policy, observation, _, _ = standing(h)
    human = h.verifier(h.client.build_request("GET", "/", headers=h.headers()))
    control = WorkPolicyControl(h.runtime, policy, lambda: observation)
    control.install_offline(human, "signed-skill-draft-installation", h.time + timedelta(minutes=10))
    readiness = h.readiness()
    from dataclasses import replace
    h.delivery.registration = replace(h.delivery.registration, verify_readiness=lambda: readiness.model_copy(update={
        "observed_at": h.time, "expires_at": h.time + timedelta(minutes=5)}))
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    return h, control, human


def post_draft(turn, purpose=PURPOSE, body=BODY, **extra):
    return turn.h.client.post(
        f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/save-skill-draft",
        headers={**turn.h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()},
        json={"request_id": "save-skill-draft", "purpose": purpose, "body": body, **extra},
    )


def test_prompt_profile_and_old_prefix_lock_values_and_gateway_boundary():
    raw = SKILL_DRAFT_PROMPT.encode("utf-8")
    assert len(raw) == 2638
    assert hashlib.sha256(raw).hexdigest() == "f47c84ffb238af98c9082d7e060f622052f70bc2bb796e8c845964c7fd508ad2"
    assert "Organize only the procedure written in this turn's user message" in SKILL_DRAFT_PROMPT
    assert assemble_skill_draft_prompt().record.declared_capabilities == SKILL_DRAFT_CAPABILITIES
    with pytest.raises(ValueError):
        assemble_skill_draft_prompt(capabilities=("read_context", "execute_operation", "save_result"))

    for profile, size, sha, capabilities in OLD_PROMPTS:
        assembled = assemble_prompt(profile)
        data = assembled.system_message.encode("utf-8")
        assert (len(data), hashlib.sha256(data).hexdigest()) == (size, sha)
        assert (() if assembled.record is None else assembled.record.declared_capabilities) == capabilities
        with pytest.raises(ValueError):
            assemble_prompt(profile, capabilities=(*capabilities, "save_skill_draft"))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions", object())
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Save."}],
            "tools": [{"type": "function", "function": {"name": "save_skill_draft", "parameters": {}}}]}
    with pytest.raises(Problem) as denied:
        gateway.validate(body, text_reply=True, model="gpt-4.1-mini")
    assert denied.value.body["error"]["code"] == "ASSISTANT_TOOLS_DENIED"
    gateway.validate(body, text_reply=True, model="gpt-4.1-mini", skill_draft_access=True)


def test_save_skill_draft_creates_one_unapproved_team_review_then_human_approve(assistant):
    h, control, human = configured_draft(assistant)
    turn = open_turn(h, control, human, "skill-draft-happy")
    try:
        reply = post_draft(turn)
        assert reply.status_code == 200, reply.text
        receipt = reply.json()
        assert receipt["schema_version"] == "fa-skill-draft-receipt-v1"
        assert receipt["state_scope"] == "at-original-draft-write"
        assert receipt["approved"] is False and receipt["effective_version_id"] is None
        assert receipt["visibility"] == "team" and receipt["artifact_id"] == turn.assignment.task.artifact_id
        assert receipt["version_id"] == turn.assignment.task.artifact_version
        assert receipt["source_turn_number"] == turn.assignment.task.instruction.turn_number
        assert receipt["source_user_text_sha256"] == hashlib.sha256(
            turn.assignment.task.instruction.content.encode("utf-8")).hexdigest()
        assert receipt["sha256"] == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
        saved = h.artifacts.get(A, A.team, receipt["artifact_id"])
        assert saved.payload["kind"] == "skill" and saved.payload["effective_version_id"] is None
        version = h.artifacts.version(A, A.team, receipt["artifact_id"], receipt["version_id"])
        assert version.approval is None and version.skill_purpose.description == PURPOSE
        assert version.source_work == h.runtime.profiles[turn.binding.binding_id].legacy_source_work
        assert h.artifacts.download(B, A.team, receipt["artifact_id"], receipt["version_id"], version.blob.etag)[1] == BODY.encode()
        assert h.assistant.skills.catalog(A, A.team, (receipt["artifact_id"],)).entries == ()
        reviews = h.artifacts.skill_reviews(A, A.team).model_dump(mode="json")
        entry = next(item for item in reviews["entries"] if item["artifact_id"] == receipt["artifact_id"])
        assert entry["status"] == "awaiting_decision"

        before = h.store._guard()
        assert post_draft(turn).json() == receipt and h.store._guard() == before
        assert h.artifacts.get(A, A.team, receipt["artifact_id"]) == saved
        assert post_draft(turn, body=OTHER).status_code == 409

        record = h.artifacts.get(A, A.team, receipt["artifact_id"])
        approved = h.artifacts.decide(HumanApprover(h.artifacts.approval.approver_oid, A.team), A.team,
            receipt["artifact_id"], record.etag, ApprovalInput(
                decision_id=receipt["version_id"], version_id=receipt["version_id"], sha256=receipt["sha256"],
                metadata_sha256=receipt["metadata_sha256"], decision="approve"))
        assert approved.payload["effective_version_id"] == receipt["version_id"]
        assert h.assistant.skills.catalog(A, A.team, (receipt["artifact_id"],)).entries
        settle(turn, ordinary(turn.assignment.sdk_session_id))
        archived = h.store.read_snapshot((key("text-archive", turn.binding.job_id),))[key("text-archive", turn.binding.job_id)]
        assert archived.payload["data"]["skill_draft_receipt"] == receipt
        assert h.assistant.result(turn.start.binding).known_effects == (receipt["receipt_id"],)
    finally:
        turn.client.close()


@pytest.mark.parametrize("payload", [
    {"purpose": "bad\x01purpose", "body": BODY},
    {"purpose": "x" * 513, "body": BODY},
    {"purpose": PURPOSE, "body": " \r\n\t "},
    {"purpose": PURPOSE, "body": "A" * 8193},
    {"purpose": PURPOSE, "body": "hidden\x00body"},
    {"purpose": PURPOSE, "body": BODY, "extra": "field"},
])
def test_invalid_purpose_body_or_extra_field_is_refused_before_any_write(assistant, payload):
    h, control, human = configured_draft(assistant)
    turn = open_turn(h, control, human, "skill-draft-invalid")
    try:
        before = h.store._guard()
        with patch.object(h.artifacts, "create", wraps=h.artifacts.create) as create:
            response = post_draft(turn, **payload)
        assert response.status_code == 422
        assert h.store._guard() == before and create.call_count == 0
    finally:
        turn.client.close()


@pytest.mark.parametrize("stage", ["create", "upload", "blob", "missing-blob"])
def test_unknown_create_or_upload_reconciles_only_original_ids(assistant, monkeypatch, stage):
    h, control, human = configured_draft(assistant)
    turn = open_turn(h, control, human, "skill-draft-unknown")
    native_create, native_upload, native_put = h.artifacts.create, h.artifacts.upload, h.artifacts.store.put
    counts = {"create": 0, "upload": 0, "blob": 0}

    def create(*args, **kwargs):
        counts["create"] += 1
        result = native_create(*args, **kwargs)
        if stage == "create" and counts["create"] == 1:
            raise TimeoutError("SYNTHETIC_CREATE_ACK")
        return result

    def upload(*args, **kwargs):
        counts["upload"] += 1
        result = native_upload(*args, **kwargs)
        if stage == "upload" and counts["upload"] == 1:
            raise TimeoutError("SYNTHETIC_UPLOAD_ACK")
        return result

    def put(*args, **kwargs):
        counts["blob"] += 1
        if stage == "missing-blob":
            raise TimeoutError("SYNTHETIC_MISSING_BLOB")
        result = native_put(*args, **kwargs)
        if stage == "blob" and counts["blob"] == 1:
            raise TimeoutError("SYNTHETIC_BLOB_ACK")
        return result

    monkeypatch.setattr(h.artifacts, "create", create)
    monkeypatch.setattr(h.artifacts, "upload", upload)
    monkeypatch.setattr(h.artifacts.store, "put", put)
    try:
        response = post_draft(turn)
        assert response.status_code == 503
        original = dict(counts)
        again = post_draft(turn)
        if stage == "missing-blob":
            assert again.status_code == 503 and counts == original
        else:
            assert again.status_code == 200, again.text
            assert counts == {"create": 1, "upload": 1, "blob": 1}
    finally:
        turn.client.close()


@pytest.mark.parametrize("identity", ["old-profile", "revoked", "peer", "other-team"])
def test_wrong_profile_revoked_policy_or_wrong_agent_cannot_write(assistant, signing_key, jwk_client, identity):
    h = assistant
    if identity == "old-profile":
        from test_work_policy_lifecycle import enabled
        h, control, human, _ = enabled(h)
        install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
            installation=lambda: None, require_current_native_evidence=lambda: None))
    else:
        h, control, human = configured_draft(h)
    turn = open_turn(h, control, human, "skill-draft-denied")
    try:
        if identity == "revoked":
            control.revoke(human, "skill-draft-revoked")
        headers = h.headers("agent")
        if identity in {"peer", "other-team"}:
            headers = {"Authorization": "Bearer " + token(signing_key, {
                "oid": B.oid if identity == "peer" else C.oid, "idtyp": "app", "roles": ["Behavior.Agent"]},
                remove=("scp",))}
        headers["x-behavior-assistant-ticket"] = turn.client.ticket.get_secret_value()
        before = h.store._guard()
        response = h.client.post(
            f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant/sessions/{turn.assignment.session_id}/save-skill-draft",
            headers=headers, json={"request_id": "save-skill-draft", "purpose": PURPOSE, "body": BODY})
        assert response.status_code == 403
        assert h.store._guard() == before
    finally:
        turn.client.close()


def test_tool_argument_and_receipt_models_are_strict():
    SkillDraftToolArgs(purpose=PURPOSE, body=BODY)
    SaveSkillDraftCommand(request_id="save-skill-draft", purpose=PURPOSE, body=BODY)
    outcome = SkillDraftOutcome(schema_version="fa-skill-draft-outcome-v1", receipt_id="r",
                                intent_sha256="f" * 64, stage="create_unknown")
    assert outcome.known_effects == () and outcome.unresolved_operations == ("r",)
    with pytest.raises(ValueError):
        SkillDraftToolArgs.model_validate({"purpose": PURPOSE, "body": BODY, "target": "x"})
    with pytest.raises(ValueError):
        SkillDraftReceipt.model_validate({"schema_version": "fa-skill-draft-receipt-v1", "approved": True})
