"""Approved-purpose metadata through the original signed assignment and read seams."""

import json
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_api import create_behavior_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_skill_purpose_prompt import SkillPurposePromptAssemblyRecord
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from approved_skill_api_helpers import change_artifact
from assistant_api_helpers import assistant_api
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_artifacts import V1
from test_m3_auth import jwk_client, signing_key, token
from test_skill_purpose import PROFILE, PURPOSE, create_skill, upload_purpose
from work_policy_lifecycle_helpers import enabled
from skill_purpose_api_helpers import configured_purpose, add_purpose_skill


def test_revocation_during_read_never_returns_loaded_body_or_purpose(
    assistant_api, signing_key, jwk_client, monkeypatch,
):
    h = assistant_api
    add_purpose_skill(h, signing_key, jwk_client)
    entry = h.assistant.skills.catalog(
        A, A.team, ("receipt-guide",), schema_version="fa-approved-skill-catalog-v2").entries[0]
    original = h.artifacts.store.read
    def revoke(*args):
        data = original(*args)
        change_artifact(h, lambda value: value.update(effective_version_id=None))
        return data
    monkeypatch.setattr(h.artifacts.store, "read", revoke)
    before = h.store._guard()
    with pytest.raises(Problem) as refused:
        h.assistant.skills.read(A, A.team, ("receipt-guide",), entry)
    assert refused.value.status == 403
    assert refused.value.body["error"]["code"] == "SKILL_SOURCE_CHANGED"
    assert refused.value.body["error"]["message"] == "skill approval changed during the read"
    refusal = json.dumps(refused.value.body)
    assert PURPOSE not in refusal and "PURPOSE_BODY_CANARY" not in refusal
    assert h.store._guard() == before


def gateway(h, token, send):
    provider = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
                                 SimpleNamespace(get_token=token), transport=httpx.MockTransport(send))
    h.app = create_behavior_app(h.runtime, h.verifier)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    h.client = TestClient(h.app)


@pytest.mark.parametrize("field,value", [
    ("schema_version", "fa-skill-purpose-prompt-assembly-v99"), ("profile", "fa-instruction-skills-v1"),
    ("capability_catalog_version", "fa-instruction-tools-skills-v1"), ("prefix_sha256", "0" * 64),
    ("prefix_utf8_bytes", 1), ("prefix_tokens", 0), ("declared_capabilities", ["read_skill", "shell"]),
    ("system_message", "MODEL_AUTHORITY_MARKER"),
])
def test_new_prompt_record_rejects_unknown_version_or_forged_capabilities(field, value):
    record = assemble_prompt(PROFILE).record.model_dump(mode="json")
    record[field] = value
    with pytest.raises(ValueError):
        SkillPurposePromptAssemblyRecord.model_validate_json(json.dumps(record))


def test_new_catalog_preserves_four_entries_and_4096_byte_fail_closed_bound(
    assistant_api, signing_key, jwk_client,
):
    h = assistant_api
    names = ("receipt-guide", "guide-two", "guide-three", "guide-four")
    for name in names:
        add_purpose_skill(h, signing_key, jwk_client, name=name, description="Purpose.")
    catalog = h.assistant.skills.catalog(A, A.team, names, schema_version="fa-approved-skill-catalog-v2")
    assert len(catalog.entries) == 4 and len(catalog.model_dump_json().encode()) <= 4096
    for name in names:
        change_artifact(h, lambda value: value.update(title="\U0001f4d8" * 256), name)
    with pytest.raises(Problem) as denied:
        h.assistant.skills.catalog(A, A.team, names, schema_version="fa-approved-skill-catalog-v2")
    assert denied.value.status == 413
    assert denied.value.body["error"]["code"] == "SKILL_CATALOG_LIMIT"
    assert "receipt-guide" not in json.dumps(denied.value.body)
    profile = {**next(iter(h.runtime.profiles.values())).model_dump(), "prompt_profile": PROFILE}
    AssistantProfile.model_validate({**profile, "skill_artifacts": names})
    with pytest.raises(ValueError):
        AssistantProfile.model_validate({**profile, "skill_artifacts": (*names, "fifth")})


def test_new_skill_description_does_not_change_the_8192_byte_body_boundary(assistant_api, signing_key, jwk_client):
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    h = assistant_api
    text = ('"\\' * 4096).encode()
    add_purpose_skill(h, signing_key, jwk_client, body=text, description='"' * 512)
    entry = h.assistant.skills.catalog(
        A, A.team, ("receipt-guide",), schema_version="fa-approved-skill-catalog-v2").entries[0]
    result = h.assistant.skills.read(A, A.team, ("receipt-guide",), entry)
    assert len(result.text.encode()) == 8192 and result.text.encode() == text
    boundary = AssistantModelBoundary(None, text_reply=True, skill_access=True, skill_purpose=True)
    encoded = boundary.tool_result("read_skill", result.model_dump_json())
    assert json.loads(encoded)["text"].encode() == text and len(encoded.encode()) > 16384
    with pytest.raises(ValueError):
        AssistantModelBoundary(None, text_reply=True, skill_access=True).tool_result(
            "read_skill", result.model_dump_json())


def test_a_profile_cannot_name_an_unknown_purpose_profile_version(assistant_api):
    original = next(iter(assistant_api.runtime.profiles.values())).model_dump()
    with pytest.raises(ValueError):
        AssistantProfile.model_validate({**original, "prompt_profile": "fa-instruction-skill-purpose-v99"})
