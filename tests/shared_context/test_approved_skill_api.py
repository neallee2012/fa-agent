"""Signed local API and original authority/client path, with synthetic M3 storage."""

import copy
import json
from types import MappingProxyType
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import A
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_native_capability_wire import NATIVE_CAPABILITY_PREFIX
from shared_context.behavior_skill_prompt import SkillPromptAssemblyRecord, assemble_prompt
from shared_context.errors import Problem
from shared_context.m3_models import CreateArtifact
from assistant_api_helpers import assistant_api
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from agent_runtime_routing_helpers import agent_headers
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from test_native_agent_api import relay_case
from test_native_capability_api import BODY as NATIVE_BODY, SKILLS, skilled, skilled_http, started
from work_policy_lifecycle_helpers import enabled
from approved_skill_api_helpers import add_skill, BODY, configured, change_artifact, approve_skill_version

# A lambda, not a def: the parametrize id stays "<lambda>4".
_other_approver = lambda value: value["versions"][0]["approval"].update(approver_oid="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.mark.parametrize("change", [
    lambda value: value.update(effective_version_id=None),
    lambda value: value.update(kind="sop"),
    lambda value: value["versions"][0].update(source_work="different-source"),
    lambda value: value["versions"][0].update(source_work_revision=2),
    _other_approver,
])
def test_current_source_or_approval_change_denies_the_original_catalog_and_read(assistant_api, change):
    # The per-turn catalog fence was the old session's; the shared reader still refuses the original entry.
    h = assistant_api
    add_skill(h)
    entry = h.assistant.skills.catalog(A, A.team, ("receipt-guide",)).entries[0]
    assert h.assistant.skills.read(A, A.team, ("receipt-guide",), entry).text == BODY
    change_artifact(h, change)
    guard = h.store._guard()
    if change is _other_approver:
        with pytest.raises(Problem) as catalog_denied:
            h.assistant.skills.catalog(A, A.team, ("receipt-guide",))
        assert catalog_denied.value.status == 403
        assert catalog_denied.value.body["error"]["code"] == "SKILL_APPROVAL_DENIED"
    else:
        assert entry not in h.assistant.skills.catalog(A, A.team, ("receipt-guide",)).entries
    with pytest.raises(Problem) as denied:
        h.assistant.skills.read(A, A.team, ("receipt-guide",), entry)
    assert denied.value.status == 403
    assert denied.value.body["error"]["code"] in {"SKILL_SOURCE_CHANGED", "SKILL_APPROVAL_DENIED"}
    assert h.store._guard() == guard


@pytest.mark.parametrize("fault", ["bytes", "blob-error", "lookup-error", "during-read"])
def test_source_failures_do_not_return_a_prior_body_or_mutate_authority(assistant_api, monkeypatch, fault):
    h = assistant_api
    add_skill(h)
    entry = h.assistant.skills.catalog(A, A.team, ("receipt-guide",)).entries[0]
    assert h.assistant.skills.read(A, A.team, ("receipt-guide",), entry).text == BODY
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_SOURCE_ERROR_CANARY")
    if fault == "bytes":
        monkeypatch.setattr(h.artifacts.store, "read", lambda *args: b"CORRUPTED_BODY_CANARY")
    elif fault == "blob-error":
        monkeypatch.setattr(h.artifacts.store, "read", fail)
    elif fault == "lookup-error":
        monkeypatch.setattr(h.artifacts.repository, "get", fail)
    else:
        read = h.artifacts.store.read
        def revoke_during_read(*args):
            data = read(*args)
            change_artifact(h, lambda value: value.update(effective_version_id=None))
            return data
        monkeypatch.setattr(h.artifacts.store, "read", revoke_during_read)
    guard = h.store._guard()
    # A raw storage failure propagates unchanged (the API's generic handler answers 503 without its text).
    raw = fault in {"blob-error", "lookup-error"}
    with pytest.raises(RuntimeError if raw else Problem) as refused:
        h.assistant.skills.read(A, A.team, ("receipt-guide",), entry)
    if raw:
        assert str(refused.value) == "PRIVATE_SOURCE_ERROR_CANARY"
    else:
        assert refused.value.status in {403, 502, 503}
        assert "CANARY" not in json.dumps(refused.value.body) and "text" not in refused.value.body
    if fault == "during-read":
        # The recheck after the body read (m3_skills ApprovedSkillSource.read), not the first lookup.
        assert refused.value.body["error"]["message"] == "skill approval changed during the read"
    assert h.store._guard() == guard


@pytest.mark.parametrize("name", ["../receipt-guide", "C:\\private\\SKILL.md", "https://private.invalid/skill"])
def test_catalog_name_cannot_be_a_path_or_url(skilled_http, signing_key, name):
    from shared_context.native_capability_api import install_native_capability_routes
    (case, refs), (loop, _) = skilled_http
    scope = started(case, (SKILLS,), skills=refs).model_dump(mode="json")
    app = FastAPI()
    _install_error_handlers(app)
    install_native_capability_routes(app, case.base.a, enabled=True)
    agent = agent_headers(case.base.c, signing_key, 0)
    guard = case.base.a.runtime.store._guard()
    with patch.object(case.skills, "read", wraps=case.skills.read) as read,             TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        refused = client.post(NATIVE_CAPABILITY_PREFIX + "/read-skill", json={**scope, "name": name}, headers=agent)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "BEHAVIOR_INVALID_REQUEST"
        assert NATIVE_BODY not in refused.text and read.call_count == 0
        assert case.base.a.runtime.store._guard() == guard
        allowed = client.post(NATIVE_CAPABILITY_PREFIX + "/read-skill", json={**scope, "name": "receipt-guide"},
                              headers=agent)
        assert allowed.status_code == 200 and allowed.json()["text"] == NATIVE_BODY and read.call_count == 1


def test_catalog_byte_limit_is_an_explicit_error_not_truncation(assistant_api):
    h = assistant_api
    names = ("receipt-guide", "guide-two", "guide-three", "guide-four")
    for name in names:
        add_skill(h, name)
        change_artifact(h, lambda value: value.update(title="\U0001f4d8" * 256), name)
    with pytest.raises(Problem) as denied:
        h.assistant.skills.catalog(A, A.team, names)
    assert denied.value.status == 413
    assert denied.value.body["error"]["code"] == "SKILL_CATALOG_LIMIT"
    refusal = json.dumps(denied.value.body)
    assert "receipt-guide" not in refusal and "CANARY" not in refusal


@pytest.mark.parametrize("selected,profile", [
    (("receipt-guide",), None), (("receipt-guide",), "fa-instruction-v1"),
    (("receipt-guide", "receipt-guide"), "fa-instruction-skills-v1"),
    (("one", "two", "three", "four", "five"), "fa-instruction-skills-v1"),
    (("../private",), "fa-instruction-skills-v1"),
])
def test_server_configuration_rejects_ambiguous_unbounded_or_disabled_selectors(assistant_api, selected, profile):
    original = next(iter(assistant_api.runtime.profiles.values())).model_dump()
    with pytest.raises(ValueError):
        AssistantProfile.model_validate({**original, "prompt_profile": profile, "skill_artifacts": selected})


def test_factory_configuration_pins_skill_selectors_without_new_actor_or_storage_scope():
    from shared_context.behavior_server_config import ServerConfiguration
    from test_behavior_assistant_server import assistant_configuration
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    raw["assistant"]["profiles"][0].update(
        prompt_profile="fa-instruction-skills-v1", skill_artifacts=["receipt-guide"])
    after = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert before.sha256 != after.sha256
    assert before.resource_bindings()["assistant_sha256"] != after.resource_bindings()["assistant_sha256"]
    assert before.identity == after.identity and before.table == after.table and before.sources == after.sources
    assert after.assistant.profiles[0].skill_artifacts == ("receipt-guide",)


@pytest.mark.parametrize("field,value", [
    ("system_message", "PRIVATE_SYSTEM_CANARY"), ("prefix_tokens", 0),
    ("prefix_sha256", "0" * 64), ("prefix_utf8_bytes", 1), ("source", "model-attested"),
    ("declared_capabilities", ["read_skill", "shell"]), ("omissions", ["PRIVATE_REASONING_CANARY"]),
])
def test_skill_prompt_metadata_rejects_private_fields_false_counts_and_attestation(field, value):
    record = assemble_prompt("fa-instruction-skills-v1").record.model_dump(mode="json")
    record[field] = value
    with pytest.raises(ValueError):
        SkillPromptAssemblyRecord.model_validate_json(json.dumps(record))


def test_reader_preserves_the_exact_8k_body_even_when_json_escaping_doubles_transport_size(assistant_api):
    h = assistant_api
    text = '"\\' * 4096
    add_skill(h, text=text)
    entry = h.assistant.skills.catalog(A, A.team, ("receipt-guide",)).entries[0]
    result = h.assistant.skills.read(A, A.team, ("receipt-guide",), entry)
    assert len(result.text.encode("utf-8")) == 8192 and result.text == text
    assert len(result.model_dump_json().encode("utf-8")) > 16384
