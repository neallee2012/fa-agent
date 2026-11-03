"""Skill tool requirements are approved metadata, never an installer or capability grant."""

import hashlib

import pytest
from pydantic import ValidationError

from conftest import A
from shared_context.behavior_capabilities import available_grants
from shared_context.errors import Problem
from shared_context.m3_models import SkillPurpose, SkillToolRequirements, VersionInput
from shared_context.m3_skills import ApprovedSkillSource
from test_m3_artifacts import m3, V1, V2
from test_skill_purpose import BODY, PURPOSE, create_skill, decide, upload_purpose


def requirements():
    grant = available_grants(None, ("fa-delegate-v1",))["fa-delegate-v1"]
    return {"schema_version": "fa-skill-tool-requirements-v1",
            "entries": [{"entry_id": grant.entry_id, "entry_sha256": grant.entry_sha256, "tool": "task"}]}


def upload(artifacts, *, tools=None, version=V1, created=None):
    created = created or create_skill(artifacts, "tool-guide", title="Tool requirements guide")
    return upload_purpose(artifacts, created.etag, version=version, name="tool-guide",
                          tools=None if tools is None else SkillToolRequirements.model_validate(tools))


def test_tool_requirements_need_exact_metadata_approval_and_a_new_immutable_version(m3):
    artifacts, _, _ = m3
    first = upload(artifacts)
    original = first.payload["versions"][0]
    assert "skill_tools" not in original
    approved = decide(artifacts, first, original, name="tool-guide")
    second = upload(artifacts, tools=requirements(), version=V2, created=approved)
    current = second.payload["versions"][-1]
    assert current.get("skill_tools") == requirements()
    assert current["sha256"] == original["sha256"]
    assert current["metadata_sha256"] != original["metadata_sha256"]
    with pytest.raises(Problem) as refused:
        decide(artifacts, second, current, name="tool-guide", metadata_sha256=original["metadata_sha256"])
    assert refused.value.status == 409
    accepted = decide(artifacts, second, current, name="tool-guide")
    assert accepted.payload["versions"][0] == approved.payload["versions"][0]
    with pytest.raises(Problem) as reused:
        upload(artifacts, version=V2, created=accepted)
    assert reused.value.status == 409
    source = ApprovedSkillSource(artifacts)
    entry = source.catalog(A, A.team, ("tool-guide",), schema_version="fa-approved-skill-catalog-v2").entries[0]
    for resources in (False, True):
        with pytest.raises(Problem) as old_reader:
            source.read(A, A.team, ("tool-guide",), entry, include_resources=resources)
        assert old_reader.value.body["error"]["code"] == "SKILL_ACCESS_DISABLED"
    read = source.read(A, A.team, ("tool-guide",), entry,
                       include_resources=True, include_tool_requirements=True)
    assert read.schema_version == "fa-approved-skill-read-v4"
    assert read.tool_requirements.model_dump(mode="json") == requirements()
    assert read.tool_availability is None


@pytest.mark.parametrize("change", [
    {"entry_id": "unknown-plugin"},
    {"entry_id": "fa-shell-v1", "tool": "run_command"},
    {"tool": "../script.ps1"},
    {"tool": "https://outside.invalid/install"},
    {"script": "install.ps1"},
    {"authorization": True},
    {"entry_sha256": "a" * 64},
])
def test_unsupported_script_path_or_grant_is_refused_before_upload(m3, monkeypatch, change):
    artifacts, _, blobs = m3
    value = requirements()
    value["entries"][0].update(change)

    def no_upload(*_args):
        pytest.fail("Unsupported tool metadata must not reach blob storage")

    monkeypatch.setattr(blobs, "put", no_upload)
    created = create_skill(artifacts, "tool-guide", title="Tool requirements guide")
    with pytest.raises((ValidationError, Problem)) as refused:
        upload(artifacts, tools=value, created=created)
    assert not isinstance(refused.value, Problem) or refused.value.status == 422
    document = artifacts.get(A, A.team, "tool-guide").payload
    assert document["pending"] is None and document["versions"] == []


def test_complete_tool_manifest_is_kept_without_changing_old_empty_bytes(m3):
    artifacts, _, _ = m3
    tools = SkillToolRequirements.model_validate(requirements())
    assert SkillToolRequirements.from_header(tools.header()) == tools
    old = VersionInput(filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(BODY).hexdigest())
    assert old.model_dump_json() == (
        '{"filename":"guide.md","media_type":"text/markdown","sha256":"'
        + hashlib.sha256(BODY).hexdigest() + '","source_work":null}')
    metadata = VersionInput(**old.model_dump(), skill_tools=tools,
                            skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE))
    created = create_skill(artifacts, "client-tool-guide", title="Tool guide")
    artifacts.upload(A, A.team, "client-tool-guide", V1, created.etag, metadata, BODY)
    assert artifacts.version(A, A.team, "client-tool-guide", V1).skill_tools == tools
    with pytest.raises(ValueError):
        SkillToolRequirements.model_validate({**requirements(), "entries": requirements()["entries"] * 2})
    with pytest.raises(ValueError):
        VersionInput(**old.model_dump(), skill_tools=tools)
