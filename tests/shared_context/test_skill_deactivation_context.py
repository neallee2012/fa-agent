"""Real human deactivation through captured sources and fresh SDK turns."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import httpx
import pytest
from copilot import CopilotClient
from fastapi.testclient import TestClient

from conftest import A
from shared_context import behavior_assistant_hosted as hosted
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_sdk_compaction import events_from
from shared_context.behavior_skill_prompt import assemble_prompt
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.source_materials import digest
from sdk_checkpoint_local import sse
from test_approved_skill_api import post_skill
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_prompt import saved_turn, submit
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn
from test_skill_guidance_authority import (
    BODY, NAME, PROFILES, PURPOSE, approval_command, configure_guidance,
    configure_read_operation, load_guidance, operation_path, prepare_operation, protected_rows,
)
from test_skill_purpose import signed_m3
from test_skill_purpose_api import add_purpose_skill


def deactivate_original(h, signing_key, jwk_client, source):
    http, agent, human = signed_m3(h.artifacts, signing_key, jwk_client)
    path = "/teams/" + A.team + "/artifacts/" + NAME
    try:
        before = http.get(path, headers=human)
        response = http.post(path + "/deactivations", headers={**human, "If-Match": before.headers["etag"]}, json={
            "schema_version": "fa-skill-deactivation-v1", "decision_id": "d" * 32,
            "version_id": source["version_id"], "sha256": source["sha256"],
            "metadata_sha256": source["metadata_sha256"],
        })
        assert response.status_code == 200, response.text
        assert response.json()["effective_version_id"] is None
        assert response.json()["versions"] == before.json()["versions"]
        assert http.get(path + "/effective", headers=agent).status_code == 404
        return response.json()["deactivations"][0]
    finally:
        http.close()


@pytest.mark.parametrize("profile", PROFILES)
def test_loaded_skill_deactivation_fences_sources_not_independent_operation_grants(
        assistant, signing_key, jwk_client, profile):
    external = configure_read_operation(assistant, signing_key, jwk_client)
    h, control, human, source = configure_guidance(assistant, signing_key, jwk_client, profile)
    receipts, calls, at_effect = [], [], {}

    def change_source(_scope):
        assert not receipts
        at_effect.update(h.store.read_snapshot((control.cost_key,)))
        receipts.append(deactivate_original(h, signing_key, jwk_client, source))
        return SimpleNamespace(token="OFFLINE_DEACTIVATION_PROVIDER")

    def forbidden_provider(_request):
        calls.append(True)
        raise AssertionError("A deactivated source must not be sent to the provider")

    provider = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=change_source), transport=httpx.MockTransport(forbidden_provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client
    turn = open_turn(h, control, human, "skill-deactivation-current")
    try:
        loaded = load_guidance(turn, source, profile)
        prepared, _ = prepare_operation(turn)
        approved = h.client.post(operation_path(turn) + "/approve", headers=h.headers(),
                                content=approval_command(turn, prepared).model_dump_json())
        assert approved.status_code == 200
        original_operation = protected_rows(turn)
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [
            {"role": "assistant", "tool_calls": [{"id": "original-skill", "type": "function",
                "function": {"name": "read_skill", "arguments": json.dumps({"name": NAME})}}]},
            {"role": "tool", "tool_call_id": "original-skill", "content": loaded.model_dump_json()},
            {"role": "user", "content": "Use only current permitted sources."},
        ]}
        original_id = digest(body)
        later_body = {**body, "messages": [*body["messages"], {"role": "user", "content": "Original second reservation."}]}
        later_id = digest(later_body)
        assert turn.client.reserve_model(original_id).state == "accepted"
        assert turn.client.reserve_model(later_id).state == "accepted"
        denied = post_skill(turn, {"request_id": original_id, "body": body}, action="model")
        assert denied.status_code == 403
        assert denied.headers["x-behavior-model-upstream-error-code"] == "SKILL_SOURCE_CHANGED"
        assert len(receipts) == 1 and calls == []
        assert h.store.read_snapshot((control.cost_key,)) == at_effect
        preserved_keys = (key("session", turn.assignment.session_id),
                          key("model-intent", turn.assignment.session_id, original_id),
                          key("model-intent", turn.assignment.session_id, later_id), control.cost_key)
        preserved = h.store.read_snapshot(preserved_keys)
        assert preserved[preserved_keys[0]].payload["data"]["skill_catalog"]["entries"][0]["version_id"] == source["version_id"]
        assert preserved[preserved_keys[1]].payload["data"]["consumed"] is True
        assert preserved[preserved_keys[2]].payload["data"]["consumed"] is False
        assert preserved[control.cost_key].payload["data"]["model_pending_microusd"] > 0
        catalog = post_skill(turn, {"request_id": "current-skills"}, action="skill-catalog")
        read = post_skill(turn, {"request_id": "read-skill", "name": NAME})
        fresh_id = digest({"deactivation": "new reservation must remain unadmitted"})
        reserve = post_skill(turn, {"request_id": fresh_id, "request_sha256": fresh_id}, action="reserve-model")
        consume = post_skill(turn, {"request_id": later_id, "body": later_body}, action="model")
        repeat = post_skill(turn, {"request_id": original_id, "body": body}, action="model")
        assert [response.status_code for response in (catalog, read, reserve, consume, repeat)] == [403] * 5
        assert "SK06_UNTRUSTED_GUIDANCE_CANARY" not in read.text
        assert h.store.read_snapshot(preserved_keys) == preserved
        assert protected_rows(turn) == original_operation and external == [] and calls == []
        assert h.assistant.skills.catalog(A, A.team, (NAME,)).entries == ()
        fresh_key = key("model-intent", turn.assignment.session_id, fresh_id)
        assert h.store.read_snapshot((fresh_key,))[fresh_key] is None
    finally:
        turn.client.close()


def test_fixed_sdk_deactivation_keeps_history_but_requires_a_new_approved_version(
        assistant, signing_key, jwk_client, monkeypatch):
    h, control, human, source = configure_guidance(assistant, signing_key, jwk_client, PROFILES[1])
    prompt = assemble_prompt(PROFILES[1])
    current_bodies = (BODY.decode("utf-8"), None, "NEW_APPROVED_SKILL_VERSION_CANARY\n")
    versions = (source["version_id"], None, "2" * 32)
    phase, calls, bridges, receipts, forced = 0, [], [], [], []
    original_init = hosted.AssistantSession.__init__
    original_force = CopilotClient.force_stop

    def observe_bridge(instance, *args, **kwargs):
        original_init(instance, *args, **kwargs)
        bridges.append(instance)

    monkeypatch.setattr(hosted.AssistantSession, "__init__", observe_bridge)

    async def observe_force(instance):
        forced.append(True)
        return await original_force(instance)

    monkeypatch.setattr(CopilotClient, "force_stop", observe_force)

    def provider(request):
        body = json.loads(request.content)
        number = 1 + sum(item["turn"] == phase for item in calls)
        assert number <= 2
        tools = {tool["function"]["name"]: tool["function"] for tool in body["tools"]}
        assert set(tools) == {"read_context", "read_skill", "execute_operation"}
        assert prompt.system_message in [item["content"] for item in body["messages"] if item["role"] == "system"]
        catalog = json.loads(tools["read_skill"]["description"].rsplit("\n", 1)[-1])
        expected = {} if versions[phase] is None else {NAME: versions[phase]}
        assert {entry["name"]: entry["version_id"] for entry in catalog["entries"]} == expected
        assert catalog["unavailable_count"] == int(phase == 1)
        name_schema = tools["read_skill"]["parameters"]["properties"]["name"]
        if expected:
            assert set(name_schema["enum"]) == set(expected)
        else:
            assert "enum" not in name_schema
        if phase == 0 and number == 1:
            assert "SK06_UNTRUSTED_GUIDANCE_CANARY" not in json.dumps(body)
        if phase:
            old = [item for item in body["messages"] if item["role"] == "tool"
                   and item.get("tool_call_id") == "deactivation-read-0"]
            assert len(old) == 1 and "SK06_UNTRUSTED_GUIDANCE_CANARY" in json.dumps(old)
        if phase < 2 or number == 1:
            assert "NEW_APPROVED_SKILL_VERSION_CANARY" not in json.dumps(body)
        if number == 1:
            delta = {"tool_calls": [{"index": 0, "id": f"deactivation-read-{phase}", "type": "function",
                "function": {"name": "read_skill", "arguments": json.dumps({"name": NAME})}}]}
        else:
            result = [item for item in body["messages"] if item["role"] == "tool"][-1]
            assert result["tool_call_id"] == f"deactivation-read-{phase}"
            content = result["content"]
            text = content if isinstance(content, str) else "".join(part["text"] for part in content)
            if phase == 1:
                assert "Authority refused" in text and "CANARY" not in text
            else:
                loaded = json.loads(text)
                assert loaded["text"] == current_bodies[phase]
                assert loaded["skill"]["version_id"] == versions[phase]
            delta = {"content": "Synthetic current-skill observation saved."}
        calls.append({"turn": phase, "request": number, "catalog_versions": expected,
                      "catalog_sha256": hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()})
        return sse(request, delta, len(calls))

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_SKILL_DEACTIVATION")),
        transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    conversation, sdk_id, old_events, archives = None, None, [], {}
    texts = ("  Read the approved skill.\r\n", "  The original skill is deactivated.\r\n",
             "  Read only the newly approved version.\r\n")
    with TemporaryDirectory(prefix="deact-") as temporary:
        for phase in range(3):
            if phase == 1:
                receipts.append(deactivate_original(h, signing_key, jwk_client, source))
            if phase == 2:
                add_purpose_skill(h, signing_key, jwk_client, name=NAME, body=current_bodies[2].encode(),
                                  description=PURPOSE, version=versions[2], create=False)
            h.executor.root = Path(temporary) / str(phase)
            accepted, _ = submit(h, "skill-deactivate-" + str(phase), texts[phase], conversation)
            conversation = accepted["conversation_id"]
            done = h.runtime.controller.step(accepted["binding_id"])
            assert done.state == "succeeded" and done.result_forwarded
            binding = h.runtime.bindings[accepted["binding_id"]]
            saved = saved_turn(h, binding.job_id)
            assert {item.name: item.version_id for item in saved.skill_catalog.entries} == (
                {} if phase == 1 else {NAME: versions[phase]})
            assert saved.prompt_assembly == prompt.record
            state = bridges[-1]._session_files.snapshot()
            assert sdk_id is None or state.sdk_session_id == sdk_id
            sdk_id = state.sdk_session_id
            events = events_from(state.files)
            assert [item["data"]["content"] for item in events if item["type"] == "user.message"] == list(texts[:phase + 1])
            retained = [item for item in events if item["type"] in {"user.message", "tool.execution_complete"}]
            assert retained[:len(old_events)] == old_events
            old_events = retained
            assert not any(item["type"] in {"skill.invoked", "session.compaction_start"} for item in events)
            archive_key = key("text-archive", binding.job_id)
            archives[archive_key] = h.store.read_snapshot((archive_key,))[archive_key]
            assert h.store.read_snapshot(tuple(archives)) == archives
    assert len(calls) == 6 and len(bridges) == 3 and len(receipts) == 1 and not forced
    assert all(bridge._sdk_stop.graceful and bridge._sdk_stop.runtime_version == "1.0.83" for bridge in bridges)
    assert importlib.metadata.version("github-copilot-sdk") == "1.0.13"
    if os.environ.get("FA_SKILL_DEACTIVATION_EVIDENCE_DIR"):
        target = Path(os.environ["FA_SKILL_DEACTIVATION_EVIDENCE_DIR"])
        target.mkdir(parents=True, exist_ok=True)
        (target / "native-lifecycle.json").write_text(json.dumps({
            "scope": "Real human M3 deactivation and later new-version approval; fixed SDK with offline provider",
            "requests": calls, "stops": [bridge._sdk_stop.model_dump(mode="json") for bridge in bridges],
            "source_version_retained": source["version_id"], "deactivation_decision_id": receipts[0]["decision_id"],
            "historical_sdk_events_retained": True, "same_conversation_and_sdk": True,
            "raw_user_bytes_retained": True, "prior_archives_unchanged": True,
            "deactivation_is_not_history_erasure": True, "no_native_skill_events_observed": True,
            "force_stop_calls": len(forced),
        }, indent=2) + "\n", encoding="utf-8")
