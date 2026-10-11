"""Skill tool requirements are approved metadata, never an installer or capability grant."""

import base64
import hashlib
import json

import pytest

from conftest import A
from shared_context.behavior_capabilities import available_grants
from shared_context.errors import Problem
from shared_context.m3_client import ArtifactClient
from shared_context.m3_models import CreateArtifact, SkillPurpose, SkillToolRequirements, VersionInput
from shared_context.m3_skills import ApprovedSkillSource
from test_m3_artifacts import m3, V1, V2
from test_m3_auth import signing_key, jwk_client
from test_skill_purpose import BODY, PURPOSE, signed_m3


def requirements():
    grant = available_grants(None, ("fa-delegate-v1",))["fa-delegate-v1"]
    return {"schema_version": "fa-skill-tool-requirements-v1",
            "entries": [{"entry_id": grant.entry_id, "entry_sha256": grant.entry_sha256, "tool": "task"}]}


def upload(http, agent, *, tools=None, version=V1, created=None):
    path = "/teams/team-one/artifacts/tool-guide"
    created = created or http.post(path, headers=agent, json={
        "kind": "skill", "title": "Tool requirements guide", "source_work": "source-work"})
    headers = {
        **agent, "If-Match": created.headers["etag"], "Content-Type": "application/octet-stream",
        "X-Artifact-Filename": "guide.md", "X-Artifact-Media-Type": "text/markdown",
        "X-Artifact-SHA256": hashlib.sha256(BODY).hexdigest(),
        "X-Artifact-Skill-Purpose": SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE).header(),
    }
    if tools is not None:
        headers["X-Artifact-Skill-Tools"] = base64.urlsafe_b64encode(json.dumps(tools).encode()).decode()
    return path, http.put(path + "/versions/" + version, headers=headers, content=BODY)


def test_tool_requirements_need_exact_metadata_approval_and_a_new_immutable_version(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    http, agent, human = signed_m3(artifacts, signing_key, jwk_client)
    with http:
        path, first = upload(http, agent)
        assert first.status_code == 200, first.text
        original = first.json()["versions"][0]
        assert "skill_tools" not in original
        approved = http.post(path + "/decisions", headers={**human, "If-Match": first.headers["etag"]}, json={
            "decision_id": V1, "version_id": V1, "decision": "approve",
            "sha256": original["sha256"], "metadata_sha256": original["metadata_sha256"]})
        assert approved.status_code == 200, approved.text
        _, second = upload(http, agent, tools=requirements(), version=V2, created=approved)
        assert second.status_code == 200, second.text
        current = second.json()["versions"][-1]
        assert current.get("skill_tools") == requirements()
        assert current["sha256"] == original["sha256"]
        assert current["metadata_sha256"] != original["metadata_sha256"]
        refused = http.post(path + "/decisions", headers={**human, "If-Match": second.headers["etag"]}, json={
            "decision_id": V2, "version_id": V2, "decision": "approve",
            "sha256": current["sha256"], "metadata_sha256": original["metadata_sha256"]})
        assert refused.status_code == 409
        accepted = http.post(path + "/decisions", headers={**human, "If-Match": second.headers["etag"]}, json={
            "decision_id": V2, "version_id": V2, "decision": "approve",
            "sha256": current["sha256"], "metadata_sha256": current["metadata_sha256"]})
        assert accepted.status_code == 200
        assert accepted.json()["versions"][0] == approved.json()["versions"][0]
        _, reused = upload(http, agent, version=V2, created=accepted)
        assert reused.status_code == 409
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
def test_unsupported_script_path_or_grant_is_refused_before_upload(m3, signing_key, jwk_client, monkeypatch, change):
    artifacts, _, blobs = m3
    http, agent, _ = signed_m3(artifacts, signing_key, jwk_client)
    value = requirements()
    value["entries"][0].update(change)

    def no_upload(*_args):
        pytest.fail("Unsupported tool metadata must not reach blob storage")

    monkeypatch.setattr(blobs, "put", no_upload)
    with http:
        path, response = upload(http, agent, tools=value)
        assert response.status_code == 422, response.text
        document = http.get(path, headers=agent).json()
        assert document["pending"] is None and document["versions"] == []


def test_client_transports_the_complete_tool_manifest_without_changing_old_empty_bytes(m3, signing_key, jwk_client):
    artifacts, _, _ = m3
    http, agent, _ = signed_m3(artifacts, signing_key, jwk_client)
    tools = SkillToolRequirements.model_validate(requirements())
    assert SkillToolRequirements.from_header(tools.header()) == tools
    with http:
        client = ArtifactClient(http, A.team, lambda: agent["Authorization"].removeprefix("Bearer "))
        created = client.create("client-tool-guide", CreateArtifact(
            kind="skill", title="Tool guide", source_work="source-work"))
        old = VersionInput(filename="guide.md", media_type="text/markdown", sha256=hashlib.sha256(BODY).hexdigest())
        assert old.model_dump_json() == (
            '{"filename":"guide.md","media_type":"text/markdown","sha256":"'
            + hashlib.sha256(BODY).hexdigest() + '","source_work":null}')
        metadata = VersionInput(**old.model_dump(), skill_tools=tools,
                                skill_purpose=SkillPurpose(schema_version="fa-skill-purpose-v1", description=PURPOSE))
        saved = client.upload("client-tool-guide", V1, created.etag, metadata, BODY)
        assert client.version(saved, V1).skill_tools == tools
    with pytest.raises(ValueError):
        SkillToolRequirements.model_validate({**requirements(), "entries": requirements()["entries"] * 2})
    with pytest.raises(ValueError):
        VersionInput(**old.model_dump(), skill_tools=tools)


def test_cli_preview_names_the_declared_tool_without_implying_authorization(m3, signing_key, jwk_client, monkeypatch, capsys):
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from shared_context.m3_cli import main

    service, _, _ = m3
    http, agent, human = signed_m3(service, signing_key, jwk_client)
    _, response = upload(http, agent, tools=requirements())
    assert response.status_code == 200, response.text
    version = response.json()["versions"][0]
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = SimpleNamespace(token=human["Authorization"].removeprefix("Bearer "))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    previews = []

    def confirm(_):
        previews.append(capsys.readouterr().out)
        return "approve"

    with patch("shared_context.m3_cli.InteractiveBrowserCredential", return_value=credential), \
            patch("shared_context.m3_cli.httpx.Client", return_value=http), \
            patch("builtins.input", side_effect=confirm):
        result = main([
            "--api", "https://poc.example.westus3.azurecontainerapps.io",
            "--tenant", service.context.auth.tenant_id, "--audience", service.context.auth.audience,
            "--client-id", service.approval.client_id, "--team", A.team, "approve", "tool-guide",
            "--version", V1, "--sha256", version["sha256"], "--decision-id", V1,
            "--metadata-sha256", version["metadata_sha256"],
        ])
    assert result == 0 and len(previews) == 1
    assert '"skill_tools"' in previews[0] and '"authorization": false' in previews[0]
    assert "fa-delegate-v1" in previews[0] and version["metadata_sha256"] in previews[0]
