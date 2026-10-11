"""Portable references are data, never a saved permission or runtime configuration."""

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from conftest import A, C
from shared_context.behavior_assistant import AssistantProfile
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_behavior_assistant import assistant
from test_instruction_flow import runtime_path
from test_m3_auth import UNKNOWN_OID, jwk_client, signing_key, token
from test_skill_purpose import BODY, PROFILE, PURPOSE, signed_m3
from test_skill_purpose_api import add_purpose_skill
from test_work_policy_lifecycle import enabled

PATH = "/work-policy/v1/agent-template"
NAME = "receipt-guide"
PROMPT_DEFAULT_ENV = "FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE"


@pytest.fixture(autouse=True)
def isolated_prompt_default(monkeypatch):
    monkeypatch.delenv(PROMPT_DEFAULT_ENV, raising=False)


def configure(h, profile=PROFILE, selected=(NAME,)):
    h.runtime.profiles = MappingProxyType({
        key: AssistantProfile.model_validate({
            **value.model_dump(), "prompt_profile": profile, "skill_artifacts": selected,
        }) for key, value in h.runtime.profiles.items()
    })
    h, control, human, _ = enabled(h)
    h.app = create_assistant_app(h.assistant, h.verifier)
    composition = SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant),
    )
    install_work_policy_routes(h.app, composition)
    return h, control, human, composition


@pytest.fixture
def template_case(assistant, signing_key, jwk_client):
    source = add_purpose_skill(assistant, signing_key, jwk_client)
    h, control, human, composition = configure(assistant)
    with TestClient(h.app) as http:
        h.client = http
        yield h, control, human, source, composition


def export(h):
    response = h.client.get(PATH, headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    return response


def inspect(h, value):
    return h.client.post(PATH + "/inspect", json=value, headers=h.headers("read-only"))


def deactivate(h, source, signing_key, jwk_client):
    http, _, human = signed_m3(h.artifacts, signing_key, jwk_client)
    path = "/teams/team-one/artifacts/" + NAME
    with http:
        current = http.get(path, headers=human)
        response = http.post(path + "/deactivations", headers={**human, "If-Match": current.headers["etag"]}, json={
            "schema_version": "fa-skill-deactivation-v1", "decision_id": "d" * 32,
            "version_id": source["version_id"], "sha256": source["sha256"],
            "metadata_sha256": source["metadata_sha256"],
        })
        assert response.status_code == 200, response.text


def test_export_is_deterministic_references_only_and_does_not_invoke_runtime(template_case):
    h, control, _, source, _ = template_case
    guard = h.store._guard()
    artifacts = copy.deepcopy(h.artifacts.repository.data)
    with patch.object(h.artifacts.store, "read", side_effect=AssertionError("No body download")), \
            patch("subprocess.Popen", side_effect=AssertionError("No runtime process")):
        first, second = export(h), export(h)
        inspected = inspect(h, first.json())
    assert first.content == second.content
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["content-disposition"] == 'attachment; filename="fa-agent-template.json"'
    assert first.headers["x-content-sha256"] == hashlib.sha256(first.content).hexdigest()
    data = first.json()
    prompt = assemble_prompt(PROFILE).record
    assert data["schema_version"] == "fa-agent-template-v1"
    assert data["role"] == {
        "profile": PROFILE, "prefix_sha256": prompt.prefix_sha256,
        "capability_catalog_version": prompt.capability_catalog_version,
    }
    assert data["skills"] == [{
        "name": NAME, "version_id": source["version_id"], "sha256": source["sha256"],
        "metadata_sha256": source["metadata_sha256"],
    }]
    assert data["unavailable_skills"] == []
    assert data["authorization"] is False
    assert data["settings"]["mode"] == "empty"
    assert data["settings"]["sdk_version"] == "1.0.13"
    assert data["settings"]["runtime_version"] == "1.0.83"
    for value in (BODY.decode(), PURPOSE, A.oid, h.binding.tenant_id, h.binding.human_oid,
                  h.binding.binding_id, h.binding.owner.root_budget_id, h.binding.team_id,
                  h.runtime.profiles[h.binding.binding_id].source.source_id):
        assert value not in first.text
    assert inspected.status_code == 200, inspected.text
    result = inspected.json()
    assert result["template_sha256"] == first.headers["x-content-sha256"]
    assert result["references_match"] is True
    assert result["authorization"] is False and result["activated"] is False
    assert result["required_at_destination"] == data["required_at_destination"]
    assert "connector-accounts-and-tool-grants" in result["required_at_destination"]
    assert h.store._guard() == guard and h.artifacts.repository.data == artifacts
    assert control.records == {} and h.endpoint.requests == []


@pytest.mark.parametrize("claims,remove,status", [
    ({"oid": A.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, ("scp",), 403),
    ({"oid": C.oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, ("scp",), 403),
    ({"oid": UNKNOWN_OID}, (), 403), ({"appid": UNKNOWN_OID}, (), 403),
    ({"scp": "Behavior.Approve"}, (), 403), ({"scp": "Artifacts.Approve"}, (), 403),
    ({"tid": UNKNOWN_OID}, (), 401), ({"exp": 1}, (), 401),
])
def test_export_and_inspection_require_current_designated_human_read(
        template_case, signing_key, claims, remove, status):
    h, _, _, _, _ = template_case
    data = export(h).json()
    encoded = token(signing_key, {"scp": "Behavior.Read", **claims}, remove=remove)
    headers = {"Authorization": "Bearer " + encoded}
    before = h.store._guard()
    assert h.client.get(PATH, headers=headers).status_code == status
    assert h.client.post(PATH + "/inspect", json=data, headers=headers).status_code == status
    assert h.store._guard() == before and h.endpoint.requests == []


@pytest.mark.parametrize("change", [
    {"credentials": "PRIVATE_TEMPLATE_CANARY"}, {"team": "team-two"},
    {"authorization": True}, {"approved": True}, {"schema_version": "future"},
    {"required_at_destination": []},
])
def test_imported_fields_cannot_supply_authority_or_private_configuration(template_case, change):
    h, _, _, _, _ = template_case
    data = {**export(h).json(), **change}
    guard = h.store._guard()
    response = inspect(h, data)
    assert response.status_code == 422
    assert "PRIVATE_TEMPLATE_CANARY" not in response.text
    assert h.store._guard() == guard and h.endpoint.requests == []


def test_deactivation_keeps_exported_history_but_inspection_requires_current_source(
        template_case, signing_key, jwk_client):
    h, _, _, source, _ = template_case
    original = export(h).json()
    deactivate(h, source, signing_key, jwk_client)
    current = export(h).json()
    assert current["skills"] == [] and current["unavailable_skills"] == [NAME]
    outcome = inspect(h, original)
    assert outcome.status_code == 200
    assert outcome.json()["skills"] == [{"name": NAME, "status": "unavailable"}]
    assert outcome.json()["references_match"] is False
    assert not outcome.json()["authorization"] and not outcome.json()["activated"]
    assert original["skills"][0]["version_id"] == source["version_id"]


def test_new_approval_does_not_rebind_an_exported_version(template_case, signing_key, jwk_client):
    h, _, _, _, _ = template_case
    original = export(h).json()
    add_purpose_skill(h, signing_key, jwk_client, create=False, version="2" * 32,
                      description="Changed purpose with unchanged body.")
    current = export(h).json()
    assert original["skills"][0]["sha256"] == current["skills"][0]["sha256"]
    assert original["skills"][0]["metadata_sha256"] != current["skills"][0]["metadata_sha256"]
    inspected = inspect(h, original).json()
    assert inspected["skills"] == [{"name": NAME, "status": "version_changed"}]
    assert not inspected["references_match"] and not inspected["activated"]
    assert inspect(h, current).json()["references_match"] is True


def test_inspection_cannot_select_an_unconfigured_skill(template_case, signing_key, jwk_client):
    h, _, _, _, _ = template_case
    source = add_purpose_skill(h, signing_key, jwk_client, name="not-selected")
    original = export(h).json()
    original["skills"] = [{
        "name": "not-selected", "version_id": source["version_id"],
        "sha256": source["sha256"], "metadata_sha256": source["metadata_sha256"],
    }]
    result = inspect(h, original)
    assert result.status_code == 200
    assert result.json()["skills"] == [{"name": "not-selected", "status": "unavailable"}]
    assert not result.json()["selection_matches"] and not result.json()["references_match"]
    assert [item["name"] for item in export(h).json()["skills"]] == [NAME]


@pytest.mark.parametrize("section,key,value", [
    ("role", "prefix_sha256", "0" * 64),
    ("settings", "sdk_start_profile", "fa-sdk-start-gate-v1"),
])
def test_reference_comparison_does_not_apply_imported_settings(template_case, section, key, value):
    h, _, _, _, _ = template_case
    original = export(h).json()
    changed = copy.deepcopy(original)
    changed[section][key] = value
    inspected = inspect(h, changed)
    assert inspected.status_code == 200
    assert not inspected.json()["references_match"] and not inspected.json()["activated"]
    assert export(h).json() == original


def test_revoked_standing_read_blocks_both_routes(template_case):
    h, control, human, _, _ = template_case
    original = export(h).json()
    control.revoke(human, "template-read-revoked")
    before = h.store._guard()
    assert h.client.get(PATH, headers=h.headers()).status_code == 403
    assert inspect(h, original).status_code == 403
    assert h.store._guard() == before and h.endpoint.requests == []


def test_catalog_change_during_export_is_reported_not_a_mixed_snapshot(
        template_case, signing_key, jwk_client):
    h, _, _, source, _ = template_case
    catalog = h.assistant.skills.catalog
    changed = []

    def race(*args, **kwargs):
        result = catalog(*args, **kwargs)
        if not changed:
            changed.append(True)
            deactivate(h, source, signing_key, jwk_client)
        return result

    with patch.object(h.assistant.skills, "catalog", side_effect=race):
        response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_TEMPLATE_SOURCE_CHANGED"
    assert "skills" not in response.json() and h.endpoint.requests == []


def test_late_standing_revocation_cannot_release_an_export(template_case):
    h, control, human, _, _ = template_case
    catalog = h.assistant.skills.catalog
    changed = []

    with ThreadPoolExecutor(max_workers=1) as writer:
        def race(*args, **kwargs):
            result = catalog(*args, **kwargs)
            if not changed:
                changed.append(True)
                writer.submit(Context().run, control.revoke, human, "template-read-race").result(timeout=10)
            return result

        with patch.object(h.assistant.skills, "catalog", side_effect=race):
            response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 403 and h.endpoint.requests == []


@pytest.mark.parametrize("method,path,variant", [
    ("GET", PATH + "?team=team-two", "empty"),
    ("GET", PATH, "body"),
    ("POST", PATH + "/inspect?activate=true", "valid"),
    ("POST", PATH + "/inspect", "duplicate"),
])
def test_template_routes_reject_ambiguous_input(template_case, method, path, variant):
    h, _, _, _, _ = template_case
    body = b"{}" if variant == "body" else b""
    if method == "POST":
        body = export(h).content
        assert inspect(h, json.loads(body)).status_code == 200
        if variant == "duplicate":
            body = b'{"schema_version":"fa-agent-template-v1",' + body[1:]
    response = h.client.request(method, path, content=body,
                                headers={**h.headers(), "Content-Type": "application/json"})
    assert response.status_code == 422


def test_unconfigured_policy_exports_the_new_conversation_v2_default(assistant):
    h, _, _, _ = configure(assistant, profile=None, selected=())
    with TestClient(h.app) as client:
        response = client.get(PATH, headers=h.headers())
    assert response.status_code == 200
    assert response.json()["role"]["profile"] == "fa-instruction-v2"
    assert response.json()["authorization"] is False


def test_export_uses_current_new_conversation_default_without_changing_policy(assistant, monkeypatch):
    h, control, _, _ = configure(assistant, profile=None, selected=())
    monkeypatch.setenv(PROMPT_DEFAULT_ENV, "fa-instruction-v1")
    original = control.policy.model_dump_json()
    guard = h.store._guard()
    artifacts = copy.deepcopy(h.artifacts.repository.data)
    with TestClient(h.app) as client, \
            patch.object(h.artifacts.store, "read", side_effect=AssertionError("No body download")), \
            patch("subprocess.Popen", side_effect=AssertionError("No runtime process")):
        h.client = client
        first, second = export(h), export(h)
        result = inspect(h, first.json())
        assert first.content == second.content
        expected = assemble_prompt("fa-instruction-v1").record
        assert first.json()["role"] == {
            "profile": expected.profile, "prefix_sha256": expected.prefix_sha256,
            "capability_catalog_version": expected.capability_catalog_version,
        }
        assert first.json()["skills"] == [] and first.json()["authorization"] is False
        assert result.status_code == 200 and result.json()["references_match"] is True
        assert result.json()["authorization"] is False and result.json()["activated"] is False
        monkeypatch.delenv(PROMPT_DEFAULT_ENV)
        without_override = client.get(PATH, headers=h.headers())
        assert without_override.status_code == 200
        assert without_override.json()["role"]["profile"] == "fa-instruction-v2"
    assert control.policy.model_dump_json() == original
    assert h.store._guard() == guard and h.artifacts.repository.data == artifacts
    assert control.records == {} and h.endpoint.requests == []


@pytest.mark.parametrize("default", ["fa-instruction-v1", "unknown"])
def test_export_keeps_explicit_policy_ahead_of_default(template_case, monkeypatch, default):
    h, _, _, _, _ = template_case
    original = export(h).content
    monkeypatch.setenv(PROMPT_DEFAULT_ENV, default)
    assert export(h).content == original
    assert inspect(h, json.loads(original)).json()["references_match"] is True


def test_export_refuses_invalid_needed_default_without_effects(assistant, monkeypatch):
    h, control, _, _ = configure(assistant, profile=None, selected=())
    guard = h.store._guard()
    monkeypatch.setenv(PROMPT_DEFAULT_ENV, "PRIVATE_TEMPLATE_DEFAULT_CANARY")
    with TestClient(h.app) as client:
        response = client.get(PATH, headers=h.headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INSTRUCTION_DEFAULT_PROMPT_CONFIGURATION"
    assert "PRIVATE_TEMPLATE_DEFAULT_CANARY" not in response.text
    assert h.store._guard() == guard and control.records == {} and h.endpoint.requests == []


def test_missing_authority_is_an_explicit_disabled_capability(template_case):
    h, _, _, _, composition = template_case
    composition.assistant.authority = None
    response = h.client.get(PATH, headers=h.headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AGENT_TEMPLATE_DISABLED"


def test_inspection_checks_the_exact_complete_byte_limit(template_case):
    from shared_context.behavior_agent_template import MAX_TEMPLATE_BYTES
    h, _, _, _, _ = template_case
    raw = export(h).content
    limit = raw + b" " * (MAX_TEMPLATE_BYTES - len(raw))
    assert len(limit) == MAX_TEMPLATE_BYTES
    headers = {**h.headers("read-only"), "Content-Type": "application/json"}
    assert h.client.post(PATH + "/inspect", content=limit, headers=headers).status_code == 200
    assert h.client.post(PATH + "/inspect", content=limit + b" ", headers=headers).status_code == 413


@pytest.mark.parametrize("count,status", [(4, 200), (5, 422)])
def test_imported_skill_references_have_a_complete_bounded_selection(template_case, count, status):
    h, _, _, _, _ = template_case
    data = export(h).json()
    reference = data["skills"][0]
    data["skills"] = [{**reference, "name": f"missing-{i}"} for i in range(count)]
    result = inspect(h, data)
    assert result.status_code == status
    if status == 200:
        assert len(result.json()["skills"]) == count
        assert not result.json()["references_match"] and not result.json()["activated"]


@pytest.mark.parametrize("change", ["duplicate", "overlapping-unavailable", "nested-secret"])
def test_template_rejects_ambiguous_references_and_nested_private_fields(template_case, change):
    h, _, _, _, _ = template_case
    data = export(h).json()
    if change == "duplicate":
        data["skills"] *= 2
    elif change == "overlapping-unavailable":
        data["unavailable_skills"] = [NAME]
    else:
        data["settings"]["credential"] = "PRIVATE_TEMPLATE_CANARY"
    response = inspect(h, data)
    assert response.status_code == 422 and "PRIVATE_TEMPLATE_CANARY" not in response.text


def test_template_dto_and_api_import_without_hosted_sdk():
    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    program = r"""
import importlib.abc, sys
class NoHostedSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "copilot" or fullname.startswith("copilot."):
            raise ModuleNotFoundError("Hosted SDK forbidden in API", name=fullname)
sys.meta_path.insert(0, NoHostedSdk())
sys.path.insert(0, sys.argv[1])
from shared_context.behavior_agent_template import AgentTemplate, export_template
from shared_context.behavior_work_policy_api import install_work_policy_routes
assert AgentTemplate.model_json_schema() and callable(export_template) and callable(install_work_policy_routes)
assert not any(name == "copilot" or name.startswith("copilot.") for name in sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", program, str(source)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
