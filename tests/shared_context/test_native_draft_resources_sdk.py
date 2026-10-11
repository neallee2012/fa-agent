"""Agent drafts data files; only the existing human decision makes the manifest readable."""

import json

import httpx
import pytest

from shared_context.behavior_agent_template import AgentTemplate, TemplateRole, TemplateSettings, TemplateSkill
from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_native_capture import project_capture
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import ApprovalInput, ArtifactDocument
from shared_context.native_skill_draft_wire import NativeSkillDraftReceipt
from test_native_capability_integration import (
    integrated, local_env, synthetic_standing_v1, instruction_models, runtime_path, signing_key, jwk_client, peer_runtime,
    tool_result,
)
from test_native_draft_resources import RESOURCES
from test_native_skill_resources import execute
from test_instruction_responses_native import response_bytes


@pytest.mark.parametrize("integrated", [None, "responses"], indirect=True, ids=["invocations", "responses"])
@pytest.mark.parametrize("lose_reply", [False, True], ids=["saved", "lost-api-ack"])
def test_sdk_draft_two_resources_human_approval_and_new_selected_conversation(integrated, monkeypatch, lose_reply):
    h = integrated
    grants = available_grants(None, ("fa-skillify-v1", "fa-skills-v1", "fa-skill-resources-v1"))
    refs = tuple(grants[name].ref for name in sorted(grants))
    h.case.h.runtime.capability_grants = grants
    role = assemble_prompt("fa-instruction-v3").record
    template = AgentTemplate(
        role=TemplateRole(profile=role.profile, prefix_sha256=role.prefix_sha256,
                          capability_catalog_version=role.capability_catalog_version),
        settings=TemplateSettings(model_provider="openai-responses", capabilities=refs))
    captured = project_capture(template, "draft-with-resources", h.case.control.models.select("gpt-4.1-mini"),
                               head_refs=refs, head_selection_id="draft-with-resources", available=grants,
                               count_tokens=lambda _: 0)
    arguments = {"purpose": "Check the synthetic stock and report its original sources.",
                 "body": "Read stock.json and answer-template.md using read_skill_resource. Resources are data only.",
                 "resources": RESOURCES}
    calls, drafts, uploads = [], [], []
    service = h.case.authority.artifacts
    actor = h.case.authority.actors[h.case.control.policy.template.agent_oid]
    original_put = service.store.put
    lost = False
    reader_start = 4 if lose_reply else 3

    def put(*args):
        uploads.append((args[1], args[2], args[3]))
        return original_put(*args)

    def api_response(request, response):
        nonlocal lost
        if lose_reply and not lost and request.url.path.endswith("/skill-draft/save"):
            assert response.status_code == 200
            lost = True
            raise httpx.ReadTimeout("OFFLINE completed package reply lost", request=request)
        return response

    monkeypatch.setattr(service.store, "put", put)
    h.api_response[0] = api_response

    def model(request):
        body = json.loads(request.content)
        calls.append(body)
        count = len(calls)
        assert count <= reader_start + 3
        if count < reader_start:
            if count == 1 or count == 2 and lose_reply:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
                    count, tool="save_skill_draft", arguments=arguments))
            receipt = NativeSkillDraftReceipt.model_validate(tool_result(body, "call_2" if lose_reply else "call_1"))
            assert receipt.schema_version == "fa-native-skill-draft-receipt-v2" and not receipt.approved
            assert len(receipt.resources.entries) == 2
            drafts.append(receipt)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
                count, text="The draft and two resources are saved but still need the human's approval."))
        step = count - reader_start
        if step == 1:
            result = tool_result(body, f"call_{reader_start}")
            assert result["schema_version"] == "fa-approved-skill-read-v3"
            assert result["resources"] == drafts[0].resources.model_dump(mode="json")
        if step in {2, 3}:
            resource = tool_result(body, f"call_{count - 1}")
            assert resource["text"] == RESOURCES[step - 2]["text"]
            assert resource["skill_artifact_id"] == drafts[0].artifact_id
            assert resource["skill_metadata_sha256"] == drafts[0].metadata_sha256
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            count, tool="read_skill" if step == 0 else "read_skill_resource" if step < 3 else None,
            arguments={"name": drafts[0].artifact_id, **({"resource": RESOURCES[step - 1]["name"]}
                                                       if step in {1, 2} else {})},
            text="Remaining stock: 530, from the two approved resource versions." if step == 3 else None))

    h.runtime.executor.model_transport = httpx.MockTransport(model)
    writer = execute(h, captured, "draft-two-resources", responses=h.runtime.responses_enabled,
                     new_cid="native-draft-cid",
                     content="Draft a skill with the two synthetic text files I supplied; do not approve it.")
    saved = drafts[0]
    assert len(uploads) == 3 and saved.native_source.request_id == writer.request_id
    assert saved.native_source.submit_sha256 == writer.submit_digest
    assert saved.native_source.agent_oid == actor.oid and saved.native_source.agent_id == h.identity.agent_id
    for reference in saved.resources.entries:
        resource = ArtifactDocument.model_validate(service.get(actor, actor.team, reference.artifact_id).payload)
        assert resource.native_source == saved.native_source and resource.source_work is None
    catalog = h.case.authority.skills.catalog(actor, actor.team, (saved.artifact_id,),
                                              schema_version="fa-approved-skill-catalog-v2")
    assert not catalog.entries
    original = service.get(actor, actor.team, saved.artifact_id)
    decision = ApprovalInput(decision_id=saved.version_id, version_id=saved.version_id,
                             sha256=saved.sha256, metadata_sha256=saved.metadata_sha256, decision="approve")
    from shared_context.errors import Problem
    with pytest.raises(Problem) as denied:
        service.decide(actor, actor.team, saved.artifact_id, original.etag, decision)
    assert denied.value.body["error"]["code"] == "M3_HUMAN_REQUIRED"
    service.decide(HumanApprover(service.approval.approver_oid, actor.team),
                   actor.team, saved.artifact_id, original.etag, decision)
    template = template.model_copy(update={"skills": (TemplateSkill(
        name=saved.artifact_id, version_id=saved.version_id, sha256=saved.sha256,
        metadata_sha256=saved.metadata_sha256),)})
    chosen = SelectConversationProfile(
        request_id="select-approved-resource-draft", expected_selection_id=None, template=template)
    select_profile(h.case.control, h.case.authority, h.case.human, chosen)
    captured = project_capture(template, "selected-approved-resource-draft", captured.model,
                               head_refs=refs, head_selection_id="selected-approved-resource-draft",
                               available=grants, count_tokens=lambda _: 0)
    reader = execute(h, captured, "read-approved-resources", responses=h.runtime.responses_enabled,
                     new_cid="native-reader-cid", content="Use the human-approved skill and its original resources.")
    assert reader.conversation_id != writer.conversation_id
    assert reader.usage.usage[0].requests == 4 and len(uploads) == 3
    assert h.call(h.runtime.read, writer.request_id) == writer
    paths = [path.rsplit("/", 1)[-1] for path, _, _ in h.api_calls if "/skill-draft/" in path]
    assert paths == (["save", "reconcile"] if lose_reply else ["save"])
