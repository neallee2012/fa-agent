"""Signed mid-turn deactivation and continuation; SDK state/stop are explicit fixtures."""

import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A
from shared_context import m2_copilot
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_checkpoint_privacy import jsonl_lines
from shared_context.behavior_sdk_state import SdkState
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.source_materials import digest
from test_approved_skill_api import post_skill
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_inventory, instruction_models, runtime_path
from test_instruction_prompt import saved_turn
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import event, open_turn, settle
from test_skill_deactivation_context import deactivate_original
from test_skill_guidance_authority import NAME, PROFILES, PURPOSE, configure_guidance, load_guidance
from test_skill_purpose import signed_m3
from test_skill_purpose_api import add_purpose_skill


def loaded_state_fixture(turn, loaded=None, previous=None):
    sid = turn.assignment.sdk_session_id
    task = turn.assignment.task.instruction
    history = ([event("session.start", "start", {"sessionId": sid})] if previous is None else
               [json.loads(line) for line in jsonl_lines(previous.files["events.jsonl"])])
    identity = "turn-" + str(task.turn_number)
    history.append(event("user.message", identity, {"content": task.content}, history[-1]["id"]))
    if loaded is not None:
        history.extend([
            event("tool.execution_start", identity + "-read", {
                "toolCallId": identity + "-skill", "toolName": "read_skill", "arguments": {"name": NAME},
            }, identity),
            event("tool.execution_complete", identity + "-body", {
                "toolCallId": identity + "-skill", "success": True,
                "result": {"content": loaded.model_dump_json()},
            }, identity + "-read"),
        ])
    return SdkState(sdk_session_id=sid, files={
        "events.jsonl": "".join(json.dumps(item) + "\n" for item in history),
        "workspace.yaml": "id: " + sid + "\n",
    })


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_signed_midturn_deactivation_preserves_the_failure_and_admits_current_guidance(
        assistant, signing_key, jwk_client, monkeypatch, profile, terminal):
    forbidden_attempts = []

    def forbidden_native(*args, **kwargs):
        forbidden_attempts.append("native-entry")
        pytest.fail("This API qualification must not start an SDK client or process")

    monkeypatch.setattr(m2_copilot, "CopilotClient", forbidden_native)
    monkeypatch.setattr(subprocess, "Popen", forbidden_native)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_native)
    h, control, human, source = configure_guidance(assistant, signing_key, jwk_client, profile)
    receipts, provider_calls, before_deactivation, retained, retained_receipts = [], [], {}, {}, {}
    path = "/teams/" + A.team + "/artifacts/" + NAME

    def change_source(_scope):
        assert not receipts
        before_deactivation.update(h.store.read_snapshot((control.cost_key,)))
        receipts.append(deactivate_original(h, signing_key, jwk_client, source))
        return SimpleNamespace(token="OFFLINE_MIDTURN_DEACTIVATION")

    def in_flight_provider(_request):
        # The deactivation lands after this request's consume: inside the seconds-level window it is sent once.
        provider_calls.append("in-flight-dispatch")
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"in-flight"}}]}\n\n'
                              b'data: [DONE]\n\n', headers={"content-type": "text/event-stream"})

    provider = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=change_source), transport=httpx.MockTransport(in_flight_provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    h.executor.http_client = h.client

    def retain_original(turn):
        keys = (key("text-archive", turn.binding.job_id), key("result", turn.binding.work.work_id),
                key("reply", turn.binding.job_id))
        retained.update(h.store.read_snapshot(keys))
        receipt_id = key("text-reply", turn.assignment.session_id)
        receipt = instruction_inventory(h).receipts[receipt_id]
        retained_receipts[receipt_id] = receipt

    def assert_originals():
        assert h.store.read_snapshot(tuple(retained)) == retained
        inventory = instruction_inventory(h)
        assert {identity: inventory.receipts[identity] for identity in retained_receipts} == retained_receipts
        check_history(h)

    with ExitStack() as cleanup:
        cleanup.callback(h.client.close)
        source_http, agent, _ = signed_m3(h.artifacts, signing_key, jwk_client)
        cleanup.callback(source_http.close)
        first = open_turn(h, control, human, "deactivation-prior")
        cleanup.callback(first.client.close)
        conversation = first.assignment.task.instruction.conversation_id
        first_state = loaded_state_fixture(first, load_guidance(first, source, profile))
        settle(first, first_state)
        retain_original(first)

        current = open_turn(h, control, human, "deactivation-current", conversation)
        cleanup.callback(current.client.close)
        assert current.client.restore() == first_state
        loaded = load_guidance(current, source, profile)
        current_state = loaded_state_fixture(current, loaded, first_state)
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [
            {"role": "assistant", "tool_calls": [{"id": "current-skill", "type": "function",
                "function": {"name": "read_skill", "arguments": json.dumps({"name": NAME})}}]},
            {"role": "tool", "tool_call_id": "current-skill", "content": loaded.model_dump_json()},
            {"role": "user", "content": "Use only current permitted sources."},
        ]}
        original_id = digest(body)
        later_body = {**body, "messages": [*body["messages"], {"role": "user", "content": "Later reservation."}]}
        later_id = digest(later_body)
        assert current.client.reserve_model(original_id).state == "accepted"
        assert current.client.reserve_model(later_id).state == "accepted"
        in_flight = post_skill(current, {"request_id": original_id, "body": body}, action="model")
        assert in_flight.status_code == 200
        assert len(receipts) == 1 and provider_calls == ["in-flight-dispatch"]
        assert h.store.read_snapshot((control.cost_key,)) == before_deactivation
        fresh_body = {**body, "messages": [*body["messages"], {"role": "user", "content": "After deactivation."}]}
        fenced = post_skill(current, {"request_id": digest(fresh_body), "request_sha256": digest(fresh_body)},
                            action="reserve-model")
        assert fenced.status_code == 403
        assert fenced.headers["x-behavior-assistant-error-code"] == "SKILL_SOURCE_CHANGED"
        replay = post_skill(current, {"request_id": original_id, "body": body}, action="model")
        assert replay.status_code == 409 and provider_calls == ["in-flight-dispatch"]
        assert h.store.read_snapshot((control.cost_key,)) == before_deactivation
        accounting_keys = (control.cost_key, key("model-intent", current.assignment.session_id, original_id),
                           key("model-intent", current.assignment.session_id, later_id))
        accounting = h.store.read_snapshot(accounting_keys)
        assert accounting[accounting_keys[1]].payload["data"]["consumed"] is True
        assert accounting[accounting_keys[2]].payload["data"]["consumed"] is False
        assert accounting[control.cost_key].payload["data"]["model_pending_microusd"] > 0
        deactivated = source_http.get(path, headers=agent)
        assert deactivated.status_code == 200
        original_version = deactivated.json()["versions"][0]
        assert deactivated.json()["deactivations"] == receipts

        def refused_followup(identifier):
            before = h.store._guard()
            response = h.client.post("/work-policy/v1/submit", headers=h.headers(), json={
                "request_id": identifier, "content": "Continue only after the original turn is resolved.",
                "conversation_id": conversation,
            })
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "CONVERSATION_TURN_UNRESOLVED"
            assert h.store._guard() == before
            assert h.store.read_snapshot(accounting_keys) == accounting

        refused_followup("deactivation-still-running")
        reference = current.client.save_sdk_state(current_state, current.stop)
        if terminal == "cancelled":
            cancelled = h.client.post("/work-policy/v1/bindings/" + current.binding.binding_id + "/cancel",
                headers=h.headers(), json={"request_id": "deactivation-cancel-original"})
            assert cancelled.status_code == 200 and h.workers.inspect(current.binding.job_id).cancel_requested
        failure = "cancelled" if terminal == "cancelled" else "sdk_failed"
        result = current.client.finish_failed(reference, current.stop, failure)
        refused_followup("deactivation-stop-not-reconciled")
        h.executor._complete(current.start, result.model_dump(mode="json"))
        h.workers.reconcile(current.binding.job_id, current.binding.owner)
        assert h.workers.inspect(current.binding.job_id).state == terminal
        refused_followup("deactivation-result-not-forwarded")
        outcome = h.runtime.controller.step(current.binding.binding_id)
        assert outcome.state == terminal and outcome.result_forwarded
        saved = saved_turn(h, current.binding.job_id)
        assert saved.failure == failure and saved.sdk_stop == current.stop
        assert saved.skill_catalog == saved_turn(h, first.binding.job_id).skill_catalog
        assert saved.skill_catalog.entries[0].version_id == source["version_id"]
        retain_original(current)
        assert retained[key("reply", current.binding.job_id)] is None
        assert_originals()
        assert h.store.read_snapshot(accounting_keys) == accounting

        empty = open_turn(h, control, human, "deactivation-empty-next", conversation)
        cleanup.callback(empty.client.close)
        assert empty.assignment.sdk_session_id == first.assignment.sdk_session_id
        assert empty.binding.job_id not in {first.binding.job_id, current.binding.job_id}
        assert empty.client.restore() == current_state
        catalog = empty.client.skill_catalog()
        assert catalog.entries == () and catalog.unavailable_count == 1
        denied = post_skill(empty, {"request_id": "read-skill", "name": NAME})
        assert denied.status_code == 403
        assert "SK06_UNTRUSTED_GUIDANCE_CANARY" not in denied.text
        empty_state = loaded_state_fixture(empty, previous=current_state)
        settle(empty, empty_state)
        retain_original(empty)
        assert_originals()

        new_body = b"SK09_NEWLY_APPROVED_VERSION_CANARY\n"
        revised = add_purpose_skill(h, signing_key, jwk_client, name=NAME, body=new_body,
                                   description=PURPOSE, version="2" * 32, create=False)
        latest = source_http.get(path, headers=agent)
        assert latest.status_code == 200 and latest.json()["effective_version_id"] == revised["version_id"]
        assert latest.json()["versions"][0] == original_version and latest.json()["deactivations"] == receipts
        fresh = open_turn(h, control, human, "deactivation-new-approved-next", conversation)
        cleanup.callback(fresh.client.close)
        assert fresh.assignment.sdk_session_id == first.assignment.sdk_session_id
        assert fresh.client.restore() == empty_state
        current_catalog = fresh.client.skill_catalog()
        assert [entry.version_id for entry in current_catalog.entries] == [revised["version_id"]]
        new_read = fresh.client.read_skill(NAME)
        assert new_read.skill == current_catalog.entries[0] and new_read.text.encode("utf-8") == new_body
        settle(fresh, loaded_state_fixture(fresh, new_read, empty_state))
        assert_originals()
        assert h.workers.inspect(current.binding.job_id).state == terminal
        assert h.store.read_snapshot(accounting_keys) == accounting
        assert len(receipts) == 1 and provider_calls == ["in-flight-dispatch"] and h.endpoint.requests == []
        assert not forbidden_attempts

        if os.environ.get("FA_SK09_CONTINUATION_EVIDENCE_DIR"):
            directory = Path(os.environ["FA_SK09_CONTINUATION_EVIDENCE_DIR"])
            directory.mkdir(parents=True, exist_ok=True)
            proof = {
                "layer": "signed-api-store-controller-with-sdk-state-and-stop-fixtures",
                "profile": profile, "original_terminal_state": terminal, "failure": saved.failure,
                "in_flight_model_status": in_flight.status_code,
                "next_reserve_status": fenced.status_code,
                "next_reserve_error_code": fenced.headers["x-behavior-assistant-error-code"],
                "deactivation_receipt_sha256": digest(receipts[0]),
                "deactivation_receipts_observed": len(receipts),
                "provider_transport_calls": len(provider_calls),
                "default_fixture_provider_calls": len(h.endpoint.requests),
                "forbidden_native_entry_attempts": len(forbidden_attempts),
                "original_consumed_intent": accounting[accounting_keys[1]].payload["data"]["consumed"],
                "later_intent_consumed": accounting[accounting_keys[2]].payload["data"]["consumed"],
                "pending_microusd": accounting[control.cost_key].payload["data"]["model_pending_microusd"],
                "original_cost_ledger_sha256": digest(accounting[control.cost_key].payload),
                "retained_archive_result_reply_sha256": {
                    identity: None if record is None else digest(record.payload) for identity, record in retained.items()
                },
                "retained_save_receipt_sha256": {
                    identity: digest(record.payload) for identity, record in retained_receipts.items()
                },
                "fixture_history_sha256": [first_state.sha256, current_state.sha256, empty_state.sha256],
                "current_catalog_versions": [entry.version_id for entry in current_catalog.entries],
                "empty_catalog_unavailable_count": catalog.unavailable_count,
                "limits": [
                    "Manual API lifecycle with explicit SDK-state and stop fixtures; no native runtime qualification.",
                    "No automated-start, SDK send/stop count, model quality, cloud, or external exactly-once claim.",
                    "No missing-archive, unknown upload, general unfinished-work, or attached-resource qualification.",
                ],
            }
            with (directory / (terminal + "-" + profile + ".json")).open("x", encoding="utf-8") as stream:
                json.dump(proof, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
