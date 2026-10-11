"""Verified native writer -> original M3 draft/approval -> C2; Foundry transport is offline."""

import hashlib
import copy
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import InMemoryRepository
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_native_turn_wire import NativeReceipt, material_digest
from shared_context.behavior_native_capability_wire import NativeTurnScope
from shared_context.behavior_skill_draft import SkillDraftToolArgs
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_artifacts import ArtifactService
from shared_context.m3_models import ApprovalInput
from shared_context.m3_skills import ApprovedSkillSource
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.service import ContextService
from shared_context.native_skill_draft_wire import (
    NativeSkillDraftProof, NativeSkillDraftWrite, draft_material,
)
from test_agent_runtime import peer_runtime
from test_agent_runtime_routing import agent_headers
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_native_agent_api import relay_case
from test_native_capabilities import selected_capture


BODY = "Read the original request, verify the receipt, then summarize the outcome.\n"


@pytest.fixture
def draft_case(relay_case, monkeypatch, request):
    case = relay_case
    authority = case.base.a.assistant.authority
    existing = authority.artifacts
    context = ContextService(InMemoryRepository(), existing.context.memory, existing.context.auth)
    artifacts = ArtifactService(context, InMemoryArtifactStore(context.auth.tenant_id), existing.approval)
    monkeypatch.setattr(authority, "artifacts", artifacts)
    monkeypatch.setattr(authority, "skills", ApprovedSkillSource(artifacts))
    original = case.sender
    capture = selected_capture("fa-skillify-v1", profile=getattr(request, "param", "fa-instruction-v3"))
    from shared_context.behavior_native_turn_wire import NativeSubmit
    request = NativeSubmit(command=original.command, capture=capture)
    case.sender = NativeReceipt.model_validate({
        **original.model_dump(), "capture": capture, "capture_digest": capture.sha256,
        "submit_digest": request.sha256, "status": "started", "turn_number": 1})
    case.remotes[0].records[original.request_id] = case.sender
    case.arguments = SkillDraftToolArgs(purpose="Follow the exact user-provided workflow.", body=BODY)

    def reply(command):
        current = case.remotes[0].records[command.request_id]
        if command.operation == "read":
            return current
        assert command.operation == "skill_draft_intent"
        return NativeSkillDraftProof(
            agent_id=current.agent_id, conversation_id=current.conversation_id, request_id=current.request_id,
            submit_sha256=current.submit_digest, input_digest=material_digest(draft_material(case.arguments)),
            effect_status="pending")

    case.remotes[0].reply_override = reply
    return case


@pytest.mark.parametrize("draft_case", ["fa-instruction-v3", "fa-instruction-v4", "fa-instruction-agent-role-v2",
                                        "fa-instruction-agent-role-v3", "fa-instruction-agent-role-v4"], indirect=True)
def test_native_writer_enters_team_draft_and_old_human_approval_without_a_fake_work(draft_case):
    from shared_context.native_skill_draft_api import NativeSkillDraftGateway

    case = draft_case
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    command = NativeSkillDraftWrite(
        conversation_id=case.sender.conversation_id, request_id=case.sender.request_id, arguments=case.arguments)
    receipt = api.save(case.principal, command)
    assert receipt.native_source.agent_oid == case.principal.oid
    assert receipt.native_source.request_id == case.sender.request_id
    assert receipt.sha256 == hashlib.sha256(BODY.encode()).hexdigest() and receipt.approved is False
    authority = case.base.a.assistant.authority
    actor = authority.actors[case.principal.oid]
    m3 = authority.artifacts
    human = HumanApprover(m3.approval.approver_oid, actor.team)
    listed = m3.skill_reviews(human, actor.team)
    entry = next(item for item in listed.entries if item.artifact_id == receipt.artifact_id)
    assert entry.status == "awaiting_decision" and entry.native_source == receipt.native_source
    saved = m3.get(human, actor.team, receipt.artifact_id)
    assert saved.payload.get("source_work") is None
    assert case.base.a.runtime.work_policy.records == {}
    # Publication is the existing human operation, not a continuation of the old running RID.
    m3.decide(human, actor.team, receipt.artifact_id, saved.etag, ApprovalInput(
        decision_id="4" * 32, version_id=receipt.version_id, sha256=receipt.sha256,
        metadata_sha256=receipt.metadata_sha256, decision="approve"))
    catalog = authority.skills.catalog(actor, actor.team, (receipt.artifact_id,),
                                       schema_version="fa-approved-skill-catalog-v2")
    assert catalog.entries[0].native_submit_sha256 == receipt.native_source.submit_sha256
    assert authority.skills.read(actor, actor.team, (receipt.artifact_id,), catalog.entries[0]).text == BODY
    case.remotes[0].reply_override = lambda _command: pytest.fail("reading an existing draft cannot require the old native SID")
    view = api.read(case.base.c.human, NativeTurnScope(
        conversation_id=case.sender.conversation_id, request_id=case.sender.request_id))
    assert view.state == "effective" and view.body == BODY


@pytest.mark.parametrize("failure", ["human-caller", "cancelled", "finished", "not-selected", "changed-proof", "agent-source"])
def test_native_draft_source_refusal_precedes_any_m3_write(draft_case, failure):
    from shared_context.native_skill_draft_api import NativeSkillDraftGateway
    from shared_context.behavior_native_turn_wire import NativeAgentReceive
    from test_native_turn_wire import agent_receive

    case = draft_case
    caller = case.base.c.human if failure == "human-caller" else case.principal
    values = case.sender.model_dump()
    if failure == "cancelled":
        values["cancel_requested"] = True
    elif failure == "finished":
        values.update(status="unknown", failure_code="NATIVE_TURN_CANCELLED")
    elif failure == "not-selected":
        capture = selected_capture()
        from shared_context.behavior_native_turn_wire import NativeSubmit
        command = NativeSubmit(command=case.sender.command, capture=capture)
        values.update(capture=capture, capture_digest=capture.sha256, submit_digest=command.sha256)
    elif failure == "agent-source":
        incoming = agent_receive(case.sender.request_id, case.sender.conversation_id, recipient=case.sender.agent_id)
        request = NativeAgentReceive(command=incoming.command, source=incoming.source, capture=case.sender.capture)
        values.update(command=request.command, source=request.source, submit_digest=request.sha256)
    case.remotes[0].records[case.sender.request_id] = NativeReceipt.model_validate(values)
    if failure == "changed-proof":
        reply = case.remotes[0].reply_override

        def changed(command):
            value = reply(command)
            return value.model_copy(update={"input_digest": "e" * 64}) if command.operation == "skill_draft_intent" else value

        case.remotes[0].reply_override = changed
    service = case.base.a.assistant.authority.artifacts
    before = copy.deepcopy(service.repository.data)
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    with pytest.raises(Problem):
        api.save(caller, NativeSkillDraftWrite(
            conversation_id=case.sender.conversation_id, request_id=case.sender.request_id, arguments=case.arguments))
    assert service.repository.data == before


@pytest.mark.parametrize("original_session_available", [True, False], ids=["running", "original-session-gone"])
def test_original_unknown_blob_write_reconciles_without_uploading_bytes_again(
    draft_case, monkeypatch, original_session_available,
):
    from shared_context.native_skill_draft_api import NativeSkillDraftGateway

    case = draft_case
    service = case.base.a.assistant.authority.artifacts
    actual_put, writes = service.store.put, []

    def lost_response(*args, **kwargs):
        writes.append(args[2])
        actual_put(*args, **kwargs)
        raise Problem(503, "M3_UPLOAD_UNKNOWN", "OFFLINE original upload response lost")

    monkeypatch.setattr(service.store, "put", lost_response)
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    command = NativeSkillDraftWrite(
        conversation_id=case.sender.conversation_id, request_id=case.sender.request_id, arguments=case.arguments)
    with pytest.raises(Problem) as pending:
        api.save(case.principal, command)
    assert pending.value.body["error"]["code"] == "SKILL_DRAFT_OUTCOME_UNKNOWN"
    if not original_session_available:
        case.remotes[0].reply_override = lambda _command: pytest.fail(
            "reconciling the original M3 version must not invoke the original Hosted session")
    receipt = api.reconcile(case.principal, command)
    assert len(writes) == 1
    assert api.reconcile(case.principal, command) == receipt
    assert len(writes) == 1 and receipt.sha256 == hashlib.sha256(BODY.encode()).hexdigest()


@pytest.mark.parametrize("change", ["request", "conversation", "body"])
def test_reconcile_cannot_create_another_draft_or_replace_original_bytes(draft_case, change):
    from shared_context.native_skill_draft_api import NativeSkillDraftGateway

    case = draft_case
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    command = NativeSkillDraftWrite(
        conversation_id=case.sender.conversation_id, request_id=case.sender.request_id, arguments=case.arguments)
    original = api.save(case.principal, command)
    authority = case.base.a.assistant.authority
    actor = authority.actors[case.principal.oid]
    human = HumanApprover(authority.artifacts.approval.approver_oid, actor.team)
    before = authority.artifacts.skill_reviews(human, actor.team)
    values = command.model_dump()
    if change == "body":
        values["arguments"]["body"] = "A different procedure."
    else:
        values[change + "_id"] = "another-original-id"
    case.remotes[0].reply_override = lambda _command: pytest.fail("reconcile must use only the retained M3 source")
    with pytest.raises(Problem) as refused:
        api.reconcile(case.principal, NativeSkillDraftWrite.model_validate(values))
    assert refused.value.body["error"]["code"] == (
        "SKILL_DRAFT_TARGET_CONFLICT" if change == "body" else "SKILL_DRAFT_OUTCOME_UNKNOWN")
    assert authority.artifacts.skill_reviews(human, actor.team) == before
    assert api.reconcile(case.principal, command) == original


def test_signed_draft_routes_keep_agent_write_and_owner_read_separate(request, signing_key):
    from shared_context.native_skill_draft_api import PREFIX, install_native_skill_draft_routes

    loop = asyncio.new_event_loop()
    try:
        case = request.getfixturevalue("draft_case")
        app = FastAPI()
        _install_error_handlers(app)
        install_native_skill_draft_routes(app, case.base.a, enabled=True)
        agent = agent_headers(case.base.c, signing_key, 0)
        human = case.base.c.h.headers(signing_key)
        command = NativeSkillDraftWrite(
            conversation_id=case.sender.conversation_id, request_id=case.sender.request_id, arguments=case.arguments)
        with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
            assert client.post(PREFIX + "/save", json=command.model_dump(mode="json"), headers=human).status_code == 403
            assert client.post(PREFIX + "/save", json={**command.model_dump(mode="json"),
                "native_source": {"kind": "forged"}}, headers=agent).status_code == 422
            saved = client.post(PREFIX + "/save", json=command.model_dump(mode="json"), headers=agent)
            assert saved.status_code == 200, saved.text
            path = ("/work-policy/v1/native-turns/conversations/" + command.conversation_id
                    + "/requests/" + command.request_id + "/skill-draft")
            read = client.get(path, headers=human)
            assert read.status_code == 200 and read.json()["body"] == BODY
            assert read.json()["state"] == "awaiting_decision"
            assert client.get(path, headers=agent).status_code == 403
            assert client.get(path + "?native_source=forged", headers=human).status_code == 422
    finally:
        if not loop.is_closed():
            loop.close()
