"""Signed owner reads durable public activity; Hosted/SDK are real and model HTTP is offline."""

import asyncio
import json
import os
from pathlib import Path
import threading
import time

import httpx
import pytest

from sdk_checkpoint_local import sse
from test_native_append_sdk import (
    append_sdk, native_api, synthetic_standing_v1, instruction_models,
    runtime_path, jwk_client, signing_key,
)
from test_instruction_responses_native import OPAQUE, response_bytes
from test_web_test_console_native import first, native_console, progress, send


@pytest.mark.parametrize("append_sdk", [None, "responses"], indirect=True, ids=["invocations", "responses"])
def test_original_native_activity_is_readable_while_running_and_after_completion(append_sdk):
    env = append_sdk
    held, release = threading.Event(), asyncio.Event()
    calls = []

    async def model(request):
        calls.append(json.loads(request.content))
        assert len(calls) <= 2
        if len(calls) == 1:
            delta = {"tool_calls": [{"index": 0, "id": "PRIVATE-SDK-CALL-ID", "type": "function",
                "function": {"name": "read_context", "arguments": "{}"}}]}
        else:
            held.set()
            await asyncio.wait_for(release.wait(), timeout=30)
            delta = {"content": "PUBLIC-ACTIVITY-ANSWER"}
        return sse(request, delta, len(calls))

    env.runtime.executor.model_transport = httpx.MockTransport(model)
    rid = "native-public-activity"
    try:
        accepted = env.api.client.post(env.submit_prefix + "/submit", headers=env.api.headers, json={
            "request_id": rid, "content": "Read the original context, then answer.",
        })
        assert accepted.status_code == 202, accepted.text
        assert held.wait(30), "The second model request was not reached"
        original = env.read(rid)
        cid = original["conversation_id"]
        path = "/work-policy/v1/native-turns/conversations/" + cid + "/requests/" + rid + "/progress"
        deadline = time.monotonic() + 8
        while True:
            observed = env.api.client.get(path, headers=env.api.headers)
            assert observed.status_code == 200, observed.text
            value = observed.json()
            if any(event["kind"] == "tool_completed" for event in value["progress"]["events"]):
                break
            assert time.monotonic() < deadline, "The tool completion was not saved"
            time.sleep(0.02)
        assert value["request_id"] == rid and value["conversation_id"] == cid
        assert not value["finished"]
        assert 0 < value["byte_length"] <= 8192
        assert len(observed.content) <= value["byte_length"]
        assert len(json.dumps(value, allow_nan=False).encode("utf-8")) == value["byte_length"]
        assert "PRIVATE-SDK-CALL-ID" not in observed.text
        started = next(event for event in value["progress"]["events"] if event["kind"] == "tool_started")
        completed = next(event for event in value["progress"]["events"] if event["kind"] == "tool_completed")
        assert started["tool_name"] == completed["tool_name"] == "read_context"
        assert completed["tool_start_seq"] == started["seq"]
        assert completed["summary"]
        assert env.read(rid) == original
        env.hosted.portal.call(release.set)
        saved = env.wait(rid, {"completed"})
        final = env.api.client.get(path, headers=env.api.headers)
        assert final.status_code == 200 and final.json()["finished"], final.text
        assert final.json()["progress"]["events"][:len(value["progress"]["events"])] == value["progress"]["events"]
        assert env.read(rid) == saved and len(calls) == 2
        anonymous = env.api.client.get(path)
        assert anonymous.status_code == 401
        wrong = env.api.client.get(path.replace("/" + cid + "/", "/other-cid/"), headers=env.api.headers)
        assert wrong.status_code == 403
        assert len(calls) == 2
    finally:
        env.hosted.portal.call(release.set)


@pytest.fixture
def summary_model_catalog(instruction_models):
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts["gpt-4.1-mini"]["supports_reasoning_summary"] = True
    path.write_text(json.dumps(facts), encoding="utf-8")


@pytest.mark.usefixtures("summary_model_catalog")
def test_public_summary_is_redacted_and_truncated_within_the_full_8k_envelope(append_sdk, signing_key):
    env = append_sdk
    current = env.api.client.get("/work-policy/v1/agent-template", headers=env.api.headers)
    assert current.status_code == 200, current.text
    template = current.json()
    template["settings"]["model_provider"] = "openai-responses"
    selected = env.api.client.post("/work-policy/v1/agent-template/select", headers=env.api.c.h.headers(
        signing_key, "Behavior.Read Behavior.Approve"), json={
            "request_id": "public-summary-template", "expected_selection_id": None, "template": template,
        })
    assert selected.status_code == 200, selected.text
    calls = []
    safe_text = "先核對已提供的公開資料，再逐項整理；" * 11

    def model(request):
        assert request.url.path.endswith("/responses")
        calls.append(json.loads(request.content))
        count = len(calls)
        assert count <= 14
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=response_bytes(
            count, tool="read_context" if count < 14 else None, text="完整原答案",
            public_summary=safe_text + "\nAuthorization: Bearer SECRET-TOKEN-CANARY"))

    env.runtime.executor.model_transport = httpx.MockTransport(model)
    response = env.api.client.post(env.submit_prefix + "/submit", headers=env.api.headers, json={
        "request_id": "bounded-public-summary", "content": "Check the supplied context, then answer.",
    })
    assert response.status_code == 202, response.text
    saved = env.wait("bounded-public-summary", {"completed"})
    assert saved["capture"]["reasoning_summary"] == "detailed"
    path = ("/work-policy/v1/native-turns/conversations/" + saved["conversation_id"]
            + "/requests/bounded-public-summary/progress")
    read = env.api.client.get(path, headers=env.api.headers)
    assert read.status_code == 200, read.text
    value = read.json()
    assert value["finished"] and value["truncated"] and len(calls) == 14
    assert len(read.content) <= value["byte_length"] <= 8192
    assert len(json.dumps(value, allow_nan=False).encode("utf-8")) == value["byte_length"]
    assert value["progress"]["events"][0]["seq"] > 1
    assert value["progress"]["reasoning_summary"]["availability"] == "provided"
    assert "先核對已提供的公開資料" in read.text
    assert "SECRET-TOKEN-CANARY" not in read.text and OPAQUE not in read.text
    assert "reasoning_text" not in read.text and "encrypted_content" not in read.text
    assert env.read("bounded-public-summary") == saved


@pytest.mark.parametrize("append_sdk", [None, "responses"], indirect=True, ids=["invocations", "responses"])
def test_progress_finishes_after_sdk_stop_with_200ms_store_io(append_sdk, monkeypatch):
    from azure.ai.agentserver.core.storage import FoundryStateStore

    env = append_sdk
    rid = "progress-close-after-sdk-stop"
    accepted = env.api.client.post(env.submit_prefix + "/submit", headers=env.api.headers, json={
        "request_id": rid, "content": "Finish the original answer.",
    })
    assert accepted.status_code == 202, accepted.text
    env.wait_model(rid)
    original = env.read(rid)
    path = ("/work-policy/v1/native-turns/conversations/" + original["conversation_id"]
            + "/requests/" + rid + "/progress")
    deadline = time.monotonic() + 8
    while True:
        before = env.api.client.get(path, headers=env.api.headers)
        assert before.status_code == 200, before.text
        if before.json()["recorded"]:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    delayed_writes = []
    original_write = FoundryStateStore.set_item

    async def delayed(store, key, value, **kwargs):
        if key.startswith("progress:"):
            delayed_writes.append(value["finished"])
            await asyncio.sleep(0.2)
        return await original_write(store, key, value, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", delayed)
    env.hosted.portal.call(env.release.set)
    saved = env.wait(rid, {"completed"})
    observed = env.api.client.get(path, headers=env.api.headers)
    assert observed.status_code == 200, observed.text
    assert observed.json()["finished"] and not observed.json()["unconfirmed"], observed.text
    assert delayed_writes and len(env.requests) == 1
    assert env.read(rid) == saved
    assert env.stopped[0].runtime_version == "1.0.83" and env.stopped[0].graceful


def test_one_progress_read_timeout_does_not_discard_later_steps_or_final_snapshot(append_sdk, monkeypatch):
    from azure.ai.agentserver.core.storage import FoundryStateStore

    env = append_sdk
    original_read = FoundryStateStore.get_item
    failed_reads = []

    async def timeout_once(store, key, **kwargs):
        if key.startswith("progress:") and not failed_reads:
            failed_reads.append(key)
            raise TimeoutError("OFFLINE transient progress read timeout")
        return await original_read(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "get_item", timeout_once)
    rid = "progress-final-after-read-timeout"
    accepted = env.api.client.post(env.submit_prefix + "/submit", headers=env.api.headers, json={
        "request_id": rid, "content": "Finish the original answer.",
    })
    assert accepted.status_code == 202, accepted.text
    env.wait_model(rid)
    env.hosted.portal.call(env.release.set)
    saved = env.wait(rid, {"completed"})
    path = ("/work-policy/v1/native-turns/conversations/" + saved["conversation_id"]
            + "/requests/" + rid + "/progress")
    observed = env.api.client.get(path, headers=env.api.headers)
    assert observed.status_code == 200, observed.text
    assert len(failed_reads) == 1 and len(env.requests) == 1
    assert observed.json()["finished"] and not observed.json()["unconfirmed"], observed.text
    assert len(observed.json()["progress"]["events"]) >= 3
    assert env.read(rid) == saved


@pytest.mark.parametrize("applied", [True, False], ids=["applied-ack-lost", "unresolved-put"])
def test_final_progress_requires_readback_of_an_uncertain_prior_write(append_sdk, monkeypatch, applied):
    from azure.ai.agentserver.core.storage import FoundryStateStore

    env = append_sdk
    rid = "progress-final-after-lost-ack"
    accepted = env.api.client.post(env.submit_prefix + "/submit", headers=env.api.headers, json={
        "request_id": rid, "content": "Finish the original answer.",
    })
    assert accepted.status_code == 202, accepted.text
    env.wait_model(rid)
    original = env.read(rid)
    path = ("/work-policy/v1/native-turns/conversations/" + original["conversation_id"]
            + "/requests/" + rid + "/progress")
    deadline = time.monotonic() + 8
    while True:
        before = env.api.client.get(path, headers=env.api.headers)
        assert before.status_code == 200, before.text
        if before.json()["recorded"]:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    original_write, original_read = FoundryStateStore.set_item, FoundryStateStore.get_item
    writes, failed_reads = [], []

    async def uncertain_write(store, key, value, **kwargs):
        if key.startswith("progress:"):
            writes.append(value["finished"])
            if len(writes) == 1:
                if applied:
                    await original_write(store, key, value, **kwargs)
                raise TimeoutError("OFFLINE progress write ACK lost")
        return await original_write(store, key, value, **kwargs)

    async def readback_timeout_once(store, key, **kwargs):
        if key.startswith("progress:") and writes and not failed_reads:
            failed_reads.append(key)
            raise TimeoutError("OFFLINE progress readback timeout")
        return await original_read(store, key, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", uncertain_write)
    monkeypatch.setattr(FoundryStateStore, "get_item", readback_timeout_once)
    env.hosted.portal.call(env.release.set)
    saved = env.wait(rid, {"completed"})
    observed = env.api.client.get(path, headers=env.api.headers)
    assert observed.status_code == 200, observed.text
    assert observed.json()["finished"] is applied
    assert observed.json()["unconfirmed"] is not applied
    assert writes == ([False, True] if applied else [False])
    assert len(failed_reads) == 1 and len(env.requests) == 1
    if not applied:
        assert observed.json()["progress"] == before.json()["progress"]
    assert env.read(rid) == saved


@pytest.mark.parametrize("append_sdk", [None, "responses"], indirect=True, ids=["invocations", "responses"])
@pytest.mark.parametrize("failure", ["unconfirmed", "timeout"])
def test_progress_write_failure_does_not_block_answer_or_replace_the_known_snapshot(append_sdk, monkeypatch, failure):
    from azure.ai.agentserver.core.storage import FoundryStateStore

    env = append_sdk
    rid = "progress-write-unconfirmed"
    accepted = env.api.client.post(env.submit_prefix + "/submit", headers=env.api.headers, json={
        "request_id": rid, "content": "Finish the original answer.",
    })
    assert accepted.status_code == 202, accepted.text
    env.wait_model(rid)
    original = env.read(rid)
    path = ("/work-policy/v1/native-turns/conversations/" + original["conversation_id"]
            + "/requests/" + rid + "/progress")
    deadline = time.monotonic() + 8
    while True:
        before = env.api.client.get(path, headers=env.api.headers)
        assert before.status_code == 200, before.text
        if before.json()["progress"]["events"]:
            break
        assert time.monotonic() < deadline
        time.sleep(0.02)
    writes = []
    original_write = FoundryStateStore.set_item

    async def unconfirmed(self, key, *args, **kwargs):
        if key.startswith("progress:"):
            writes.append(key)
            if failure == "timeout":
                await asyncio.sleep(30)
            raise OSError("OFFLINE progress write outcome unconfirmed")
        return await original_write(self, key, *args, **kwargs)

    monkeypatch.setattr(FoundryStateStore, "set_item", unconfirmed)
    env.hosted.portal.call(env.release.set)
    saved = env.wait(rid, {"completed"})
    after = env.api.client.get(path, headers=env.api.headers)
    assert after.status_code == 200, after.text
    assert after.json()["unconfirmed"] and not after.json()["finished"]
    assert after.json()["progress"] == before.json()["progress"]
    assert env.read(rid) == saved and len(env.requests) == 1
    assert len(writes) == 1


def test_console_retains_known_native_steps_when_the_latest_read_is_unconfirmed(native_console, monkeypatch):
    from shared_context.behavior_assistant_wire import PublicProgress
    from shared_context.native_progress_wire import progress_snapshot

    case = native_console
    draft = first(case)
    assert send(case, draft).status_code == 200
    rid = draft["request_id"]
    original = case.api.remote.records[rid]
    case.api.remote.records[rid] = original.model_copy(update={"status": "started"})
    snapshot = progress_snapshot(original, PublicProgress.model_validate_json(json.dumps({
        "events": [
            {"seq": 1, "at": "2026-10-02T00:00:00Z", "kind": "tool_started", "tool_name": "read_context",
             "tool_start_seq": 1, "status": "started", "summary": "Configured tool call started"},
            {"seq": 2, "at": "2026-10-02T00:00:01Z", "kind": "tool_completed", "tool_name": "read_context",
             "tool_start_seq": 1, "status": "succeeded", "summary": "Tool returned a result"},
        ],
    })))
    old_transport = case.api.foundry.http._transport
    refuse = False

    def transport(request):
        if json.loads(request.content).get("operation") != "progress":
            return old_transport.handle_request(request)
        if refuse:
            return httpx.Response(503, json={"error": {"code": "NATIVE_PROGRESS_UNCONFIRMED"}})
        return httpx.Response(200, content=snapshot.model_dump_json().encode("utf-8"), headers={
            "content-type": "application/json", "x-agent-session-id": original.hosted_session_id,
            "x-agent-invocation-id": "offline-public-progress",
        })

    monkeypatch.setattr(case.api.foundry.http, "_transport", httpx.MockTransport(transport))
    response = progress(case, draft["thread_id"])
    assert response.status_code == 200, response.text
    read = case.ui.client.get("/api/instructions/" + rid + "/progress")
    assert read.status_code == 200, read.text
    response = case.ui.client.get("/api/state?thread_id=" + draft["thread_id"])
    task = next(task for task in response.json()["tasks"] if task["task_id"] == rid)
    assert task["progress"]["data"]["progress"] == snapshot.progress.model_dump(mode="json")
    assert task["progress_error"] is None
    refuse = True
    case.api.remote.completed(rid)
    before_calls = len(case.calls)
    response = progress(case, draft["thread_id"])
    assert response.status_code == 200, response.text
    read = case.ui.client.get("/api/instructions/" + rid + "/progress")
    assert read.status_code == 503, read.text
    response = case.ui.client.get("/api/state?thread_id=" + draft["thread_id"])
    task = next(task for task in response.json()["tasks"] if task["task_id"] == rid)
    assert task["progress_error"] == "NATIVE_PROGRESS_UNCONFIRMED"
    assert task["progress"]["data"]["progress"] == snapshot.progress.model_dump(mode="json")
    assert task["reply"]["data"]["text"] == case.api.remote.text
    assert all(method == "GET" for method, _, _ in case.calls[before_calls:])


def test_console_reads_real_sdk_activity_separately_from_the_original_answer(native_console, append_sdk):
    case, env = native_console, append_sdk
    draft = first(case)
    calls = []

    def model(request):
        calls.append(json.loads(request.content))
        assert len(calls) <= 2
        delta = ({"tool_calls": [{"index": 0, "id": "PRIVATE-PUBLIC-ACTIVITY-CALL", "type": "function",
                  "function": {"name": "read_context", "arguments": "{}"}}]} if len(calls) == 1
                 else {"content": "The original answer is independently saved."})
        return sse(request, delta, len(calls))

    env.runtime.executor.model_transport = httpx.MockTransport(model)
    sent = send(case, draft)
    assert sent.status_code == 200, sent.text
    saved = env.wait(draft["request_id"], {"completed"})
    original = progress(case, draft["thread_id"])
    assert original.status_code == 200, original.text
    before = next(task for task in original.json()["tasks"] if task["task_id"] == draft["request_id"])
    assert before["reply"]["data"]["text"] == "The original answer is independently saved."
    assert before["progress"] is None
    received = case.ui.client.get("/api/instructions/" + draft["request_id"] + "/progress")
    assert received.status_code == 200, received.text
    assert received.json()["progress"]["finished"]
    steps = received.json()["progress"]["data"]["progress"]["events"]
    assert any(event["kind"] == "tool_completed" and event["tool_name"] == "read_context" for event in steps)
    assert "PRIVATE-PUBLIC-ACTIVITY-CALL" not in received.text
    assert env.read(draft["request_id"]) == saved and len(calls) == 2
