"""Production callbacks/API/provider with fixed SDK I/O doubles; no native or paid model."""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context import m2_copilot
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_reviewed_note_prompt import REVIEWED_NOTE_CAPABILITIES, REVIEWED_NOTE_PROMPT
from shared_context.behavior_wire import ClaimDraft, ClaimReview, Command, SourceWithdrawal
from shared_context.behavior_work_memory import WorkMemoryRead
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from shared_context.memory_bridge_settings_api import install_memory_bridge_settings_routes
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_conversation_note_memory import (
    SETTINGS, commit_note, enable_memory, memory_conversations, memory_slot_ids,
)
from test_instruction_flow import check_history, instruction_models, runtime_path, sdk_io
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary, settle


def selected_note(h, control, human):
    settings = enable_memory(h)
    first = open_turn(h, control, human, "note-producer")
    try:
        settle(first, ordinary(first.assignment.sdk_session_id))
        reference = commit_note(first)
        record = control.records[first.binding.binding_id]
        source, claim = memory_slot_ids(record.admission_id)
        selected = h.client.post(SETTINGS, headers=h.headers(), json={
            "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "use-reviewed-note",
            "expected_settings_id": settings, "enabled": True, "recall": {
                "origin_binding_id": first.binding.binding_id, "claim_id": claim,
                "expected_claim_version": reference["version"], "expected_claim_sha256": reference["sha256"],
                "use_in_new_conversations": True,
            },
        })
        assert selected.status_code == 200, selected.text
        return first.binding.binding_id, source, selected.json()["result"]["settings_id"]
    finally:
        first.client.close()


def note_sdk_io(monkeypatch):
    calls = sdk_io(monkeypatch, expected_tools=REVIEWED_NOTE_CAPABILITIES)
    factory = m2_copilot.CopilotClient.side_effect
    reads = []

    def client(**connection):
        handler = connection["request_handler"]
        options, result = {}, None

        async def request(request, context):
            body = json.loads(request.content)
            body["messages"] = [
                {"role": "system", "content": options["system_message"]["content"]}, *body["messages"],
                {"role": "assistant", "tool_calls": [{
                    "id": "selected-note", "type": "function",
                    "function": {"name": "read_reviewed_note", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": "selected-note", "content": result.text_result_for_llm},
            ]
            body["tools"] = [{"type": "function", "function": {
                "name": tool.name, "description": tool.description, "parameters": tool.parameters,
            }} for tool in options["tools"]]
            return await handler.send_request(httpx.Request(
                request.method, request.url, headers={"content-type": "application/json"}, json=body), context)

        connection["request_handler"] = SimpleNamespace(send_request=request)
        sdk = factory(**connection)
        original_open = sdk.create_session.side_effect

        async def opened(*args, **selected):
            nonlocal options
            options = selected
            session = await original_open(*args, **selected)
            original_send = session.send.side_effect
            reader = next(tool for tool in selected["tools"] if tool.name == "read_reviewed_note")
            assert reader.parameters["properties"] == {} and reader.parameters["additionalProperties"] is False
            assert selected["system_message"]["content"] == REVIEWED_NOTE_PROMPT
            assert "original inventory" not in reader.description

            async def send(prompt, **kwargs):
                nonlocal result
                result = await reader.handler(SimpleNamespace(
                    session_id=session.session_id, tool_name=reader.name, tool_call_id="selected-note", arguments={}))
                assert result.result_type == "success", result.error
                value = WorkMemoryRead.model_validate_json(result.text_result_for_llm)
                assert value.usage == "knowledge-data-not-permission" and value.text == "original inventory"
                assert value.originals[0].text == value.text
                reads.append(value)
                return await original_send(prompt, **kwargs)

            session.send.side_effect = send
            session.send_and_wait.side_effect = send
            return session

        sdk.create_session.side_effect = opened
        sdk.resume_session.side_effect = opened
        return sdk

    m2_copilot.CopilotClient.side_effect = client
    return calls, reads


def provider_routes(h, control):
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    h.endpoint.unbounded = True
    provider = AssistantProvider(h.endpoint.expected_url,
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_NOTE")),
        transport=h.endpoint.transport)
    h.client.close()
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        assistant=SimpleNamespace(authority=h.assistant), installation=lambda: control.installed(),
        require_current_native_evidence=lambda: None))
    install_memory_bridge_settings_routes(h.app, h.runtime, h.verifier)
    h.client = TestClient(h.app)
    h.executor.http_client, h.executor.model_transport = h.client, None
    h.workers.adapter = h.executor.adapter()


def test_selected_note_reaches_model_as_tool_data_and_survives_original_cold_followup(
        memory_conversations, monkeypatch, tmp_path):
    h, control, human = memory_conversations
    origin, _, setting = selected_note(h, control, human)
    provider_routes(h, control)
    calls, reads = note_sdk_io(monkeypatch)
    h.endpoint.replies = iter([{"content": "Cited note v1: original inventory."},
                               {"content": "The same selected note v1 says original inventory."}])
    first, _ = submit(h, "sdk-note-first", "Use the reviewed note.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    captured = control.records[first["binding_id"]].profile.memory_scope
    disabled = h.client.post(SETTINGS, headers=h.headers(), json={
        "schema_version": "fa-memory-bridge-settings-command-v1", "request_id": "disable-for-future-conversations",
        "expected_settings_id": setting, "enabled": False,
    })
    assert disabled.status_code == 200
    control.records.clear()
    control.refresh()
    h.executor.root = tmp_path / "cold-host"
    second, _ = submit(h, "sdk-note-followup", "Read the same original note again.", first["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    assert control.records[second["binding_id"]].profile.memory_scope == captured
    assert [call["resume"] for call in calls] == [False, True]
    assert calls[0]["sdk_session_id"] == calls[1]["sdk_session_id"]
    assert len(reads) == len(h.endpoint.requests) == 2 and reads[0] == reads[1]
    for body in h.endpoint.requests:
        assert {tool["function"]["name"] for tool in body["tools"]} == set(REVIEWED_NOTE_CAPABILITIES)
        assert body["messages"][0] == {"role": "system", "content": REVIEWED_NOTE_PROMPT}
        value = WorkMemoryRead.model_validate_json(body["messages"][-1]["content"])
        assert value == reads[0] and body["messages"][-1]["role"] == "tool"
    reply = h.client.get(f"/work-policy/v1/conversations/{first['conversation_id']}/turns/2/reply", headers=h.headers())
    assert reply.status_code == 200 and reply.json()["text"] == "The same selected note v1 says original inventory."
    check_history(h)


@pytest.mark.parametrize("change", ["source-withdrawn", "origin-read-revoked", "claim-corrected"])
def test_selected_note_refusal_prevents_cold_sdk_start_and_model_dispatch(memory_conversations, monkeypatch, change):
    h, control, human = memory_conversations
    origin, source_id, _ = selected_note(h, control, human)
    provider_routes(h, control)
    calls, _ = note_sdk_io(monkeypatch)
    h.endpoint.replies = iter([{"content": "Original note read."}])
    first, _ = submit(h, "note-before-revoke", "Read the selected note.")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    if change == "source-withdrawn":
        source = control.records[first["binding_id"]].profile.memory_scope.recall.source.sources[0]
        response = h.client.post(f"/behavior/v1/bindings/{origin}/sources/{source_id}/withdraw",
            headers=h.headers(), content=SourceWithdrawal(
                request_id="withdraw-note-source", withdrawal_id="withdraw-note-source",
                reference=source, expected_access_revision=1).model_dump_json())
        assert response.status_code == 200
    elif change == "origin-read-revoked":
        h.mutate(runtime_key("policy", origin), active=False)
    else:
        source = control.records[first["binding_id"]].profile.memory_scope.recall.source
        path = f"/behavior/v1/bindings/{origin}/claims/{source.claim_id}"
        command = ClaimDraft(request_id="human-note-correction", version=source.claim_version + 1,
            text="Corrected inventory note.", supporting=source.sources,
            effective_from=h.time, supersedes=source.claim_sha256)
        preview = h.client.post(path + "/preview", content=command.model_dump_json(), headers=h.headers())
        assert preview.status_code == 200, preview.text
        review = ClaimReview(**command.model_dump(), decision_id="human-note-correction",
            reviewed_checks=("necessity", "support", "contradiction", "sensitivity", "consent"),
            **{key: preview.json()[key] for key in ("candidate_sha256", "evidence_sha256", "policy_version")})
        assert h.client.post(path + "/review", content=review.model_dump_json(), headers=h.headers()).status_code == 200
        assert h.client.post(path + "/commit", content=Command(request_id=command.request_id).model_dump_json(),
                            headers=h.headers()).status_code == 200
    turn = open_turn(h, control, human, "note-refused-followup", first["conversation_id"])
    try:
        guard = h.store._guard()
        with pytest.raises(Problem):
            turn.client.restore()
        assert h.store._guard() == guard and len(calls) == len(h.endpoint.requests) == 1
    finally:
        turn.client.close()


@pytest.mark.parametrize("phase", ["reserve", "consume"])
@pytest.mark.parametrize("withdrawn", [False, True])
def test_job_observation_does_not_change_note_identity_but_source_withdrawal_still_fences(
        memory_conversations, monkeypatch, phase, withdrawn):
    h, control, human = memory_conversations
    origin, source_id, _ = selected_note(h, control, human)
    turn = open_turn(h, control, human, "note-observer")
    try:
        before_note = turn.client.read_reviewed_note()
        principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Use the note."}]}
        request_id = digest(body)
        reservation = aw.Reservation(request_id=request_id, request_sha256=request_id)
        if phase == "consume":
            turn.client.reserve_model(request_id)
        session = h.assistant.current_session(
            turn.binding.binding_id, turn.assignment.session_id, turn.client.ticket, principal)
        original_sources = h.assistant.model_sources
        observations = []

        def observe_after_source_read(current, reader):
            snapshot = original_sources(current, reader)
            before = h.workers.inspect(turn.binding.job_id)
            with pytest.raises(Problem) as missing_execution:
                h.workers.reconcile(turn.binding.job_id, turn.binding.owner)
            assert missing_execution.value.body["error"]["code"] == "OPERATION_RECORD_MISSING"
            after = h.workers.inspect(turn.binding.job_id)
            assert before.binding == after.binding and before.spec == after.spec
            assert after.queries_reserved == before.queries_reserved + 1
            assert turn.client.read_reviewed_note() == before_note
            observations.append(after.queries_reserved)
            if withdrawn:
                response = h.client.post(f"/behavior/v1/bindings/{origin}/sources/{source_id}/withdraw",
                    headers=h.headers(), content=SourceWithdrawal(
                        request_id="withdraw-between-read-and-commit", withdrawal_id="withdraw-between-read-and-commit",
                        reference=before_note.source.sources[0], expected_access_revision=1).model_dump_json())
                assert response.status_code == 200
            return snapshot

        monkeypatch.setattr(h.assistant, "model_sources", observe_after_source_read)

        def apply():
            if phase == "reserve":
                return h.assistant.reserve_model(session, reservation, principal)
            return h.assistant.consume_model(session, aw.ModelRequest(request_id=request_id, body=body), principal)

        if withdrawn and phase == "reserve":
            with pytest.raises(Problem) as denied:
                apply()
            assert denied.value.body["error"]["code"] == "ASSISTANT_SOURCE_CHANGED"
        else:
            apply()
        # consume no longer re-reads sources (reserve checked them under CAS), so it observes nothing here.
        assert len(observations) == (phase == "reserve") and h.endpoint.requests == []
        intent_key = key("model-intent", session.assignment.session_id, request_id)
        intent = h.store.read_snapshot((intent_key,))[intent_key]
        if phase == "reserve" and withdrawn:
            assert intent is None
        else:
            assert intent.payload["data"]["consumed"] is (phase == "consume")
    finally:
        turn.client.close()
