"""Native draft attachments keep the original Agent/source, M3 versions and human decision."""

import asyncio
import copy
import hashlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_native_capability_wire import NativeTurnScope
from shared_context.behavior_native_turn_wire import material_digest
from shared_context.behavior_skill_draft import skill_purpose
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover
from shared_context.m3_models import (
    ApprovalInput, ArtifactDocument, CreateArtifact, NativeArtifactSource, SkillPurpose,
    SkillResourceReference, SkillResources, VersionInput,
)
from shared_context.models import CreateWork
from shared_context.native_skill_draft_api import NativeSkillDraftGateway, PREFIX, install_native_skill_draft_routes
from shared_context.native_skill_draft_wire import (
    NativeSkillDraftArgs, NativeSkillDraftReceipt, NativeSkillDraftWrite, artifact_id, draft_material,
)
from test_agent_runtime_routing import agent_headers
from test_native_skill_draft_api import (
    draft_case, relay_case, peer_runtime, synthetic_standing_v1, instruction_models, signing_key, jwk_client,
)


RESOURCES = [
    {"name": "answer-template.md", "media_type": "text/markdown", "text": "Report the remaining stock and its source.\n"},
    {"name": "stock.json", "media_type": "application/json", "text": '{"total":850,"used":320}\n'},
]


def command_for(case, resources=RESOURCES):
    command = NativeSkillDraftWrite.model_validate_json(json.dumps({
        "conversation_id": case.sender.conversation_id, "request_id": case.sender.request_id,
        "arguments": {**case.arguments.model_dump(mode="json"), "resources": resources},
    }))
    case.arguments = command.arguments
    return command


def test_agent_draft_resources_share_native_source_and_require_the_original_human_approval(draft_case):
    case = draft_case
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    command = command_for(case)
    saved = api.save(case.principal, command)
    assert saved.schema_version == "fa-native-skill-draft-receipt-v2"
    assert saved.approved is False and saved.effective_version_id is None
    service = case.base.a.assistant.authority.artifacts
    actor = case.base.a.assistant.authority.actors[case.principal.oid]
    human = HumanApprover(service.approval.approver_oid, actor.team)
    skill = service.get(human, actor.team, saved.artifact_id)
    document = ArtifactDocument.model_validate(skill.payload)
    assert document.source_work is None and document.native_source == saved.native_source
    assert document.versions[0].skill_resources == saved.resources
    assert document.effective_version_id is None and document.versions[0].approval is None
    for reference, expected in zip(saved.resources.entries, RESOURCES):
        resource = ArtifactDocument.model_validate(service.get(human, actor.team, reference.artifact_id).payload)
        assert resource.kind == "deliverable" and resource.native_source == saved.native_source
        assert resource.source_work is None and resource.owner_oid == case.principal.oid
        version = resource.versions[0]
        assert version.native_source == saved.native_source and version.sha256 == reference.sha256
        assert service.download(human, actor.team, reference.artifact_id, reference.version_id, version.blob.etag)[1] == (
            expected["text"].encode("utf-8"))
    service.decide(human, actor.team, saved.artifact_id, skill.etag, ApprovalInput(
        decision_id="4" * 32, version_id=saved.version_id, sha256=saved.sha256,
        metadata_sha256=saved.metadata_sha256, decision="approve"))
    for reference, expected in zip(saved.resources.entries, RESOURCES):
        read = service.skill_resource(actor, actor.team, saved.artifact_id, saved.version_id, reference.name)
        assert read.resource == reference and read.text == expected["text"]
    view = api.read(case.base.c.human, NativeTurnScope(
        conversation_id=case.sender.conversation_id, request_id=case.sender.request_id))
    assert view.state == "effective" and view.receipt == saved
    assert case.base.a.runtime.work_policy.records == {}


@pytest.mark.parametrize("resources", [None, []], ids=["omitted", "empty"])
def test_drafts_without_resources_preserve_the_published_v1_bytes_and_ids(resources):
    values = {"purpose": "Original procedure.", "body": "Read the original.\n"}
    if resources is not None:
        values["resources"] = resources
    arguments = NativeSkillDraftArgs.model_validate(values)
    assert arguments.model_dump_json() == '{"purpose":"Original procedure.","body":"Read the original.\\n"}'
    material = draft_material(arguments)
    assert material == {
        "kind": "native-skill-draft-v1", "purpose": "Original procedure.",
        "body_sha256": "cef769a6bf599e2f6310c016e4cf58ac2a27ee375408a2e19907bbfa36a0c596"}
    assert material_digest(material) == "5051673dd56d8fc4e1225506f338c98d9e89b0ee8d85428ec7baf3e0ae5cc040"
    source = NativeArtifactSource(
        tenant_id="11111111-1111-1111-1111-111111111111", team_id="team-one", agent_id="agent-one",
        agent_oid="22222222-2222-2222-2222-222222222222", conversation_id="original-cid",
        request_id="original-rid", submit_sha256="a" * 64)
    identifier = artifact_id(source.tenant_id, source.team_id, source.agent_id, source.conversation_id, source.request_id)
    assert identifier == "d2b95fc0bc13b42a8a546eef837147561708618d1ec84db9e70624e9073b044e"
    receipt = NativeSkillDraftReceipt(
        native_source=source, artifact_id=identifier, version_id=material_digest(material)[:32],
        sha256=material["body_sha256"], byte_length=19, purpose=skill_purpose(arguments.purpose),
        metadata_sha256="b" * 64)
    assert receipt.version_id == "5051673dd56d8fc4e1225506f338c98d"
    assert hashlib.sha256(receipt.model_dump_json().encode()).hexdigest() == (
        "3b92e1c8148428d113241f952bb4af160d6e363e00fc05a3b4157f6045a0a4c4")


@pytest.mark.parametrize("resources", [
    RESOURCES * 3,
    RESOURCES[:1] * 2,
    [{**RESOURCES[0], "name": "../secret"}],
    [{**RESOURCES[0], "name": "C:\\private.txt"}],
    [{**RESOURCES[0], "name": "https://outside.invalid/file"}],
    [{**RESOURCES[0], "media_type": "application/octet-stream"}],
    [{**RESOURCES[0], "text": "x" * 8193}],
    [{**RESOURCES[0], "text": "\0"}],
    [{**RESOURCES[0], "script": "install.ps1"}],
    [{**RESOURCES[0], "native_source": {"request_id": "forged"}}],
])
def test_invalid_draft_resources_are_refused_before_any_writer(resources):
    with pytest.raises(ValueError):
        NativeSkillDraftWrite.model_validate_json(json.dumps({
            "conversation_id": "cid", "request_id": "rid",
            "arguments": {"purpose": "A procedure.", "body": "Read it.", "resources": resources}}))


@pytest.mark.parametrize("field,value", [
    ("approved", True), ("skill_tools", {"entries": [{"tool": "task"}]}),
    ("native_source", {"request_id": "invented"}),
])
def test_resource_draft_arguments_cannot_supply_approval_tools_or_source(field, value):
    with pytest.raises(ValueError):
        NativeSkillDraftArgs.model_validate_json(json.dumps({
            "purpose": "A procedure.", "body": "Read it.", "resources": RESOURCES, field: value}))


@pytest.mark.parametrize("resources", [None, [], "four", "unicode"],
                         ids=["old", "empty", "attached", "unicode-escaping"])
def test_only_attached_drafts_use_the_explicit_128k_http_envelope(request, signing_key, resources):
    loop = asyncio.new_event_loop()
    try:
        case = request.getfixturevalue("draft_case")
        unicode = resources == "unicode"
        text = "\u00e9" * 4096 if unicode else "\\" * 8192
        if resources in ("four", "unicode"):
            resources = [{"name": f"part-{number}.txt", "media_type": "text/plain", "text": text}
                         for number in range(4)]
        supplied = {"purpose": "Check original text.", "body": text}
        if resources is not None:
            supplied["resources"] = resources
        values = {"conversation_id": case.sender.conversation_id, "request_id": case.sender.request_id,
                  "arguments": supplied}
        command = NativeSkillDraftWrite.model_validate_json(json.dumps(values))
        case.arguments = command.arguments
        raw = json.dumps(values, ensure_ascii=unicode).encode("utf-8")
        limit = 131072 if resources else 32768
        if resources:
            assert len(raw) > 32768
        if unicode:
            assert len(raw) > 120 * 1024
        padded = raw + b" " * (limit - len(raw))
        app = FastAPI()
        _install_error_handlers(app)
        install_native_skill_draft_routes(app, case.base.a, enabled=True)
        headers = {**agent_headers(case.base.c, signing_key, 0), "Content-Type": "application/json"}
        service = case.base.a.assistant.authority.artifacts
        before = copy.deepcopy(service.repository.data)
        with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
            refused = client.post(PREFIX + "/save", content=padded + b" ", headers=headers)
            assert refused.status_code == 413, refused.text
            assert service.repository.data == before
            saved = client.post(PREFIX + "/save", content=padded, headers=headers)
            assert saved.status_code == 200, saved.text
            assert saved.json()["approved"] is False
            assert ("resources" in saved.json()) is bool(resources)
            read = client.post(PREFIX + "/reconcile", content=padded, headers=headers)
            assert read.status_code == 200 and read.content == saved.content
            assert client.post(PREFIX + "/reconcile", content=padded + b" ", headers=headers).status_code == 413
    finally:
        if not loop.is_closed():
            loop.close()


@pytest.mark.parametrize("failed_write", [1, 2, 3], ids=["first-resource", "last-resource", "skill-body"])
@pytest.mark.parametrize("applied", [False, True], ids=["not-visible", "ack-lost"])
def test_unknown_package_upload_only_reconciles_original_items_without_resending(
    draft_case, monkeypatch, failed_write, applied,
):
    case = draft_case
    command = command_for(case)
    service = case.base.a.assistant.authority.artifacts
    actual_put, writes = service.store.put, []

    def fail_once(*args):
        writes.append((args[1], args[2]))
        if len(writes) == failed_write:
            if applied:
                actual_put(*args)
            raise Problem(503, "M3_UPLOAD_UNKNOWN", "OFFLINE resource package write outcome unconfirmed")
        return actual_put(*args)

    monkeypatch.setattr(service.store, "put", fail_once)
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    with pytest.raises(Problem) as failure:
        api.save(case.principal, command)
    assert failure.value.body["error"]["code"] == "SKILL_DRAFT_OUTCOME_UNKNOWN"
    assert len(writes) == failed_write
    case.remotes[0].reply_override = lambda _command: pytest.fail("Reconcile must not invoke the old SDK session")
    for _ in range(2):
        if failed_write == 3 and applied:
            saved = api.reconcile(case.principal, command)
            assert saved.approved is False and len(saved.resources.entries) == 2
        else:
            with pytest.raises(Problem) as unconfirmed:
                api.reconcile(case.principal, command)
            assert unconfirmed.value.body["error"]["code"] == "SKILL_DRAFT_OUTCOME_UNKNOWN"
        assert len(writes) == failed_write


def test_another_resource_payload_cannot_replace_the_original_draft_intent(draft_case):
    case = draft_case
    command = command_for(case)
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    original = api.save(case.principal, command)
    changed = command.model_dump(mode="json")
    changed["arguments"]["resources"][0]["text"] = "A different resource."
    with pytest.raises(Problem) as conflict:
        api.reconcile(case.principal, NativeSkillDraftWrite.model_validate_json(json.dumps(changed)))
    assert conflict.value.body["error"]["code"] == "SKILL_DRAFT_TARGET_CONFLICT"
    assert api.reconcile(case.principal, command) == original


@pytest.mark.parametrize("field,value", [
    ("conversation_id", "another-conversation"), ("request_id", "another-request"), ("submit_sha256", "c" * 64),
])
def test_native_manifest_cannot_reference_another_native_source(draft_case, monkeypatch, field, value):
    case = draft_case
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    saved = api.save(case.principal, command_for(case))
    service = case.base.a.assistant.authority.artifacts
    actor = case.base.a.assistant.authority.actors[case.principal.oid]
    source = saved.native_source.model_copy(update={field: value})
    raw = b"Foreign source data.\n"
    digest = hashlib.sha256(raw).hexdigest()
    resource = service.create_native_resource(actor, actor.team, "foreign-resource", title="Foreign", native_source=source)
    service.upload(actor, actor.team, "foreign-resource", "7" * 32, resource.etag,
                   VersionInput(filename="foreign.txt", media_type="text/plain", sha256=digest), raw, native_source=source)
    draft = service.create_native_skill(actor, actor.team, "same-source-draft", title="Draft", native_source=saved.native_source)
    metadata = VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(b"Read the data.").hexdigest(),
        skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description="Use original data."),
        skill_resources=SkillResources(entries=[SkillResourceReference(
            name="foreign.txt", artifact_id="foreign-resource", version_id="7" * 32, sha256=digest)]))
    monkeypatch.setattr(service.store, "put", lambda *_: pytest.fail("Source mismatch must precede blob upload"))
    with pytest.raises(Problem) as refused:
        service.upload(actor, actor.team, "same-source-draft", "8" * 32, draft.etag, metadata, b"Read the data.",
                       native_source=saved.native_source)
    assert refused.value.body["error"]["code"] == "M3_SKILL_RESOURCE_DENIED"


@pytest.mark.parametrize("applied", [False, True])
def test_resource_create_unknown_reads_back_the_original_without_another_create(draft_case, monkeypatch, applied):
    case = draft_case
    command = command_for(case)
    service = case.base.a.assistant.authority.artifacts
    create, calls = service.repository.create, []

    def unknown_create(partition, key, payload):
        if payload.get("kind") == "deliverable":
            calls.append(key)
            if len(calls) == 1:
                if applied:
                    create(partition, key, payload)
                raise Problem(503, "STORAGE_UNAVAILABLE", "OFFLINE resource create was unconfirmed")
        return create(partition, key, payload)

    monkeypatch.setattr(service.repository, "create", unknown_create)
    api = NativeSkillDraftGateway(case.base.a, enabled=True)
    if applied:
        saved = api.save(case.principal, command)
        assert len(calls) == 2 and api.reconcile(case.principal, command) == saved
    else:
        for call in (api.save, api.reconcile):
            with pytest.raises(Problem) as unknown:
                call(case.principal, command)
            assert unknown.value.body["error"]["code"] == "SKILL_DRAFT_OUTCOME_UNKNOWN"
        assert len(calls) == 1


@pytest.mark.parametrize("native_skill", [True, False], ids=["native-skill-legacy-file", "legacy-skill-native-file"])
def test_native_and_legacy_resource_origins_cannot_be_mixed(draft_case, native_skill):
    case = draft_case
    saved = NativeSkillDraftGateway(case.base.a, enabled=True).save(case.principal, command_for(case))
    service = case.base.a.assistant.authority.artifacts
    actor = case.base.a.assistant.authority.actors[case.principal.oid]
    service.context.create(actor, actor.team, "legacy-work", CreateWork(goal="Independent legacy work"))
    raw = b"Original legacy data."
    sha = hashlib.sha256(raw).hexdigest()
    resource = service.create(actor, actor.team, "legacy-resource", CreateArtifact(
        kind="deliverable", title="Legacy", source_work="legacy-work"))
    service.upload(actor, actor.team, "legacy-resource", "6" * 32, resource.etag,
                   VersionInput(filename="legacy.txt", media_type="text/plain", sha256=sha), raw)
    if native_skill:
        skill = service.create_native_skill(actor, actor.team, "mixed-source-skill", title="Mixed",
                                            native_source=saved.native_source)
        reference = SkillResourceReference(name="legacy.txt", artifact_id="legacy-resource", version_id="6" * 32, sha256=sha)
    else:
        skill = service.create(actor, actor.team, "mixed-source-skill", CreateArtifact(
            kind="skill", title="Mixed", source_work="legacy-work"))
        reference = saved.resources.entries[0]
    metadata = VersionInput(
        filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(b"Use original data.").hexdigest(),
        skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description="Use original data."),
        skill_resources=SkillResources(entries=[reference]))
    with pytest.raises(Problem) as refused:
        service.upload(actor, actor.team, "mixed-source-skill", "8" * 32, skill.etag, metadata, b"Use original data.",
                       native_source=saved.native_source if native_skill else None)
    assert refused.value.body["error"]["code"] == "M3_SKILL_RESOURCE_DENIED"
