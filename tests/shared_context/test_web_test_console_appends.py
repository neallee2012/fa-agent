"""Thin append UI/bridge over signed owner API; only Hosted HTTP is synthetic."""

from types import SimpleNamespace
import time

import httpx
import pytest

from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
from shared_context.native_append_wire import NativeAppend, NativeAppendRead, NativeAppendReceipt, NativeAppends
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_native_turn_api import native_api
from test_web_test_console_native import first, native_console, post, progress, send
from test_native_append_sdk import append_sdk, runtime_path


@pytest.fixture
def append_console(native_console, monkeypatch):
    case = native_console
    records, calls = {}, []

    def hosted(request):
        command = NATIVE_REQUEST.validate_json(request.content)
        if not isinstance(command, (NativeAppend, NativeAppendRead)):
            return case.api.remote(request)
        calls.append(command.operation)
        original = case.api.remote.records[command.request_id]
        if isinstance(command, NativeAppend):
            receipt = records.get(command.append_id)
            if receipt is None:
                receipt = NativeAppendReceipt(
                    agent_id=original.agent_id, hosted_session_id=original.hosted_session_id,
                    request_id=original.request_id, conversation_id=original.conversation_id,
                    append_id=command.append_id, submit_digest=original.submit_digest,
                    sequence=len(records) + 1, content=command.content, status="accepted")
                records[command.append_id] = receipt
            status, result = 202, receipt
        elif command.append_id is None:
            status, result = 200, NativeAppends(
                agent_id=original.agent_id, hosted_session_id=original.hosted_session_id,
                request_id=original.request_id, conversation_id=original.conversation_id,
                submit_digest=original.submit_digest, appends=tuple(records.values()))
        elif command.append_id in records:
            status, result = 200, records[command.append_id]
        else:
            return httpx.Response(404, json={"error": {"code": "NATIVE_APPEND_MISSING"}})
        return httpx.Response(status, content=result.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": original.hosted_session_id,
            "x-agent-invocation-id": "offline-append",
        })

    monkeypatch.setattr(case.api.foundry.http, "_transport", httpx.MockTransport(hosted))
    return SimpleNamespace(case=case, records=records, calls=calls)


def test_busy_native_composer_appends_without_creating_another_human_work(append_console):
    env, case = append_console, append_console.case
    draft = first(case)
    assert send(case, draft).status_code == 200
    state = progress(case, draft["thread_id"]).json()
    assert state["composer"]["can_append"] is True
    target = state["composer"]["append_target"]
    assert target["request_id"] == draft["request_id"]
    response = post(case, "/api/instructions/" + draft["request_id"] + "/append", {
        "append_id": target["append_id"], "content": "補充同一件工作",
    })
    assert response.status_code == 200, response.text
    assert response.json()["append"]["status"] == "accepted"
    assert response.json()["append"]["request_id"] == draft["request_id"]
    updated = case.ui.console.state(draft["thread_id"])
    task = next(value for value in updated["tasks"] if value["task_id"] == draft["request_id"])
    assert task["appends"][0]["receipt_observed"] is True
    assert len(case.ui.console.instructions.store.all(draft["thread_id"])) == 1
    assert len(case.api.remote.records) == 1 and env.calls == ["append"]


def test_unknown_append_survives_restart_and_replay_only_reads_the_same_id(append_console):
    env, case = append_console, append_console.case
    draft = first(case)
    assert send(case, draft).status_code == 200
    target = progress(case, draft["thread_id"]).json()["composer"]["append_target"]
    body = {"append_id": target["append_id"], "content": "先保留這份追加"}
    case.mutate[0] = lambda request, response: (
        httpx.Response(503, json={"error": {"code": "FOUNDRY_INVOCATION_UNKNOWN"}})
        if request.url.path.endswith("/native-turns/append") else response)
    sent = post(case, "/api/instructions/" + draft["request_id"] + "/append", body)
    assert sent.status_code == 503 and env.calls == ["append"], sent.text
    case.mutate[0] = lambda request, response: response
    from web_test_console.bridge import Console
    restarted = Console(case.config, case.tmp / "native.sqlite3", backend=case.ui.console.backend,
                        clock=lambda: case.c.h.time,
                        token_provider=lambda: case.api.headers["Authorization"].removeprefix("Bearer "),
                        instruction_connection=case.connection)
    restarted.authenticate()
    restarted.instruction_progress(draft["thread_id"])
    state = restarted.state(draft["thread_id"])
    assert state["composer"]["append_target"] == {
        "request_id": draft["request_id"], "append_id": target["append_id"], "pending": True}
    assert not state["composer"]["can_append"]
    unresolved = next(value for value in state["tasks"] if value["task_id"] == draft["request_id"])
    assert unresolved["appends"][0]["receipt_observed"] is False
    observed = restarted.read_instruction_appends(draft["request_id"])
    assert observed["appends"]["appends"][0]["append_id"] == target["append_id"]
    original = restarted.journal.get(target["append_id"], "native-append")
    assert original["state"] == "UNKNOWN"
    retry = restarted.append_instruction(draft["request_id"], body["append_id"], body["content"])
    assert retry["append"]["append_id"] == target["append_id"]
    assert env.calls.count("append") == 1
    new_state = restarted.state(draft["thread_id"])
    assert new_state["composer"]["can_append"]
    assert new_state["composer"]["append_target"]["append_id"] != target["append_id"]
    task = next(value for value in new_state["tasks"] if value["task_id"] == draft["request_id"])
    assert task["appends"][0]["content"] == body["content"]
    assert task["appends"][0]["receipt_observed"] is True
    assert task["appends"][0]["submission_state"] == "UNKNOWN"


def test_append_keeps_target_version_and_rejects_changed_id_content_and_cross_origin(append_console):
    env, case = append_console, append_console.case
    draft = first(case)
    assert send(case, draft).status_code == 200
    target = progress(case, draft["thread_id"]).json()["composer"]["append_target"]
    path = "/api/instructions/" + draft["request_id"] + "/append"
    body = {"append_id": target["append_id"], "content": "原來的追加"}
    assert post(case, path, body).status_code == 200
    for changed in ({**body, "content": "偷偷換內容"}, {**body, "append_id": "another-append"}):
        result = post(case, path, changed)
        assert result.status_code == 409, result.text
    hostile = case.ui.client.post(path, json=body, headers={**case.ui.headers, "Origin": "http://elsewhere.invalid"})
    assert hostile.status_code == 403
    assert env.calls.count("append") == 1


def test_late_append_ack_after_login_changes_is_not_published(append_console):
    env, case = append_console, append_console.case
    draft = first(case)
    assert send(case, draft).status_code == 200
    target = progress(case, draft["thread_id"]).json()["composer"]["append_target"]

    def changed_login(request, response):
        if request.url.path.endswith("/native-turns/append"):
            case.ui.console.vault.clear()
        return response

    case.mutate[0] = changed_login
    response = post(case, "/api/instructions/" + draft["request_id"] + "/append", {
        "append_id": target["append_id"], "content": "保留原登入的追加",
    })
    assert response.status_code == 401, response.text
    stored = case.ui.console.journal.get(target["append_id"], "native-append")
    assert stored["state"] == "UNKNOWN" and "receipt" not in stored
    assert env.calls == ["append"]


def test_console_append_reaches_real_n1_and_fixed_sdk_without_a_new_turn(native_console, append_sdk):
    case = native_console
    draft = first(case)
    assert send(case, draft).status_code == 200
    append_sdk.wait_model(draft["request_id"])
    state = progress(case, draft["thread_id"]).json()
    target = state["composer"]["append_target"]
    response = post(case, "/api/instructions/" + draft["request_id"] + "/append", {
        "append_id": target["append_id"], "content": "追加-保留原工作",
    })
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        read = case.ui.client.get("/api/instructions/" + draft["request_id"] + "/appends")
        assert read.status_code == 200, read.text
        if read.json()["appends"]["appends"][0]["sdk_message_id"] is not None:
            break
        time.sleep(0.02)
    else:
        pytest.fail("The original SDK did not receive the console append")
    append_sdk.hosted.portal.call(append_sdk.release.set)
    completed = append_sdk.wait(draft["request_id"], {"completed"})
    assert completed["turn_number"] == 1
    page = case.ui.client.get("/api/instructions/" + draft["request_id"] + "/appends").json()["appends"]
    assert page["appends"][0]["status"] == "applied"
    updated = progress(case, draft["thread_id"]).json()
    task = next(value for value in updated["tasks"] if value["task_id"] == draft["request_id"])
    # Each SDK round's final answer is saved: the original work's answer, then the append's (#611).
    assert task["reply"]["data"]["text"] == "ORIGINAL-ANSWER\n\nAPPEND-APPLIED"
    assert task["appends"][0]["content"] == "追加-保留原工作"
    assert len(case.ui.console.instructions.store.all(draft["thread_id"])) == 1
