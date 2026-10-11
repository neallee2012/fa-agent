"""Signed owner control -> real N1 and pinned SDK; platform/model HTTP stays offline."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from azure.ai.agentserver.core.storage import FoundryStateStore

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_sessions import native_sessions
from shared_context.behavior_native_turn_wire import NativeSessionPreparation
from shared_context.behavior_wire import HostedIdentity
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key, token
from test_native_turn_api import native_api, PREFIX
from test_native_responses_api import choose_responses


POPEN = subprocess.Popen


@pytest.fixture
def append_sdk(native_api, monkeypatch, runtime_path, tmp_path, request, signing_key):
    case = native_api
    responses = getattr(request, "param", None) == "responses"
    submit_prefix = "/work-policy/v1/responses-turns" if responses else PREFIX
    if responses:
        from shared_context import behavior_native_responses_api
        choose_responses(case, signing_key)
        monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", "previous_response_id")
        monkeypatch.setattr(behavior_native_responses_api, "current_hosted_version",
                            lambda runtime, target, **_kwargs: target.agent_version)
    target = case.foundry.target
    identity = HostedIdentity(agent_id=target.agent_id, agent_name=target.agent_name,
                              agent_version=target.agent_version, session_id=target.session_id)
    root = tmp_path / "sdk-work"
    root.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        directory = root / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "AZURE_CLIENT_SECRET",
                 "AZURE_CLIENT_CERTIFICATE_PATH", "FOUNDRY_HOSTING_ENVIRONMENT", "FA_N0_STATE_PROBE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(root / "official-state"))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", identity.agent_name)
    if responses:
        monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    import _socket

    def local_only(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("Only the local SDK gateway may use sockets")
        return _socket.socket.connect(sock, address)

    def pinned_process(args, *positional, **options):
        if Path(args[0]).resolve() != runtime_path.resolve():
            raise AssertionError("Only the fixed offline SDK runtime may launch")
        return POPEN(args, *positional, **options)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(subprocess, "Popen", pinned_process)
    entered, release = threading.Event(), asyncio.Event()
    requests, stopped = [], []
    transport_state = SimpleNamespace(lose_append_ack=False, append_calls=0)
    url = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"

    async def provider(request):
        assert str(request.url) == url + "/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) > 3:
            raise AssertionError("Unexpected extra model execution")
        if len(requests) == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=60)
        serialized = json.dumps(body, ensure_ascii=False)
        answer = "APPEND-APPLIED" if "追加-保留原工作" in serialized else "ORIGINAL-ANSWER"
        base = {"id": f"offline-append-{len(requests)}", "object": "chat.completion.chunk",
                "created": 1, "model": body["model"]}
        chunks = [
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": answer},
                                   "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**base, "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
            "".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n").encode())

    async def token():
        return "OFFLINE-native-MI"

    class Executor(NativeSdkExecutor):
        async def __call__(self, *args):
            result = await super().__call__(*args)
            stopped.append(result.sdk_stop)
            return result

    executor = Executor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                        model_url=url, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(identity, executor, preflight=executor.preflight)
    executor.reader = runtime
    host = SimpleNamespace(identity=identity)
    host_options = {}
    if responses:
        from shared_context.native_responses import native_responses_handler_factory
        journal = root / "journal"
        journal.mkdir()
        host.supervisor = SimpleNamespace(journal=SimpleNamespace(root=journal))
        host_options["responses_handler_factory"] = native_responses_handler_factory
    with TestClient(create_host(host, native_runtime=runtime, **host_options)) as hosted:
        headers = {"x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": identity.session_id,
                   "x-agent-invocation-id": "OFFLINE-control", "x-agent-foundry-call-id": "OFFLINE-owner"}
        state = hosted.post("/invocations", json={"operation": "session_read"}, headers=headers).json()
        prepared = hosted.post("/invocations", json={
            "operation": "session_prepare", "request_id": "prepare-append",
            "expected_session_id": identity.session_id, "expected_etag": state["etag"],
        }, headers=headers)
        assert prepared.status_code == 200, prepared.text
        native_sessions(case.composition).prepared = NativeSessionPreparation.model_validate_json(prepared.content)

        def forward(request):
            if request.url.path.endswith("/openai/responses"):
                body = json.loads(request.content)
                streamed = body.get("stream") is True
                # The in-process TestClient buffers a whole SSE turn, so a streamed owner send is answered the way
                # the platform starts it: the same acknowledgment as its first event (RTT B; live check in dev).
                response = hosted.post("/responses", content=json.dumps({**body, "stream": False}) if streamed
                                       else request.content, headers={
                    **headers, **{key: value for key, value in request.headers.items() if key.startswith("x-client-fa-")},
                })
                if streamed and response.status_code == 200:
                    created = {"type": "response.created", "sequence_number": 0, "response": response.json()}
                    return httpx.Response(200, content=("data: " + json.dumps(created) + "\n\n").encode(),
                                          headers={"content-type": "text/event-stream"})
                return httpx.Response(response.status_code, content=response.content,
                                      headers={"content-type": "application/json"})
            assert request.url.path.endswith("/invocations") and request.method == "POST"
            response = hosted.post("/invocations", content=request.content, headers=headers)
            if json.loads(request.content)["operation"] == "append":
                transport_state.append_calls += 1
                if transport_state.lose_append_ack:
                    transport_state.lose_append_ack = False
                    raise httpx.ReadTimeout("OFFLINE native append ACK lost", request=request)
            return httpx.Response(response.status_code, content=response.content, headers={
                "content-type": "application/json", "x-agent-session-id": identity.session_id,
                "x-agent-invocation-id": "OFFLINE-control",
            })

        monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(forward))

        def read(rid):
            response = case.client.get(PREFIX + "/requests/" + rid, headers=case.headers)
            assert response.status_code == 200, response.text
            return response.json()

        def wait(rid, statuses):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                value = read(rid)
                if value["status"] in statuses:
                    return value
                time.sleep(0.03)
            pytest.fail("The original native turn did not reach its expected state")

        def wait_model(rid):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not entered.is_set():
                value = read(rid)
                if value["status"] in {"unknown", "completed", "busy_refused"}:
                    pytest.fail("Original turn ended before the first model request: "
                                + value["status"] + "/" + str(value["failure_code"]))
                time.sleep(0.02)
            assert entered.is_set(), "The first synthetic model request was not reached"

        try:
            yield SimpleNamespace(api=case, hosted=hosted, runtime=runtime, entered=entered, release=release,
                                  requests=requests, stopped=stopped, read=read, wait=wait,
                                  transport=transport_state, submit_prefix=submit_prefix, wait_model=wait_model)
        finally:
            hosted.portal.call(release.set)
            deadline = time.monotonic() + 10
            while runtime.execution_owner is not None and time.monotonic() < deadline:
                time.sleep(0.02)


def test_busy_owner_append_is_persisted_and_reaches_the_original_sdk(append_sdk):
    env = append_sdk
    accepted = env.api.client.post(PREFIX + "/submit", headers=env.api.headers,
                                   json={"request_id": "original-rid", "content": "Original work."})
    assert accepted.status_code == 202, accepted.text
    env.wait_model("original-rid")
    original = env.read("original-rid")
    assert original["status"] == "started"
    command = {
        "request_id": "original-rid", "conversation_id": original["conversation_id"], "append_id": "append-one",
        "expected_submit_digest": original["submit_digest"], "content": "追加-保留原工作",
    }
    appended = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command)
    assert appended.status_code == 202, appended.text
    assert appended.json()["append_id"] == command["append_id"] and appended.json()["request_id"] == "original-rid"
    assert env.read("original-rid")["command"] == original["command"]
    path = (PREFIX + "/conversations/" + original["conversation_id"]
            + "/requests/original-rid/appends/append-one")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        observed = env.api.client.get(path, headers=env.api.headers)
        assert observed.status_code == 200, observed.text
        if observed.json()["sdk_message_id"] is not None:
            break
        time.sleep(0.02)
    else:
        pytest.fail("The original append was not handed to the busy SDK")
    env.hosted.portal.call(env.release.set)
    completed = env.wait("original-rid", {"completed"})
    assert completed["conversation_id"] == original["conversation_id"] and completed["turn_number"] == 1
    assert len(env.requests) == 2 and "追加-保留原工作" in json.dumps(env.requests[1], ensure_ascii=False)
    applied = env.api.client.get(path, headers=env.api.headers).json()
    assert applied["status"] == "applied" and applied["delivery"] in {"steering", "queued"}
    assert env.stopped[0].runtime_version == "1.0.83" and env.stopped[0].graceful
    # Stage A 2026-10-04: every round's final answer is saved, not only the last (or the first) round's.
    result = env.api.client.get(PREFIX + "/conversations/" + original["conversation_id"]
                                + "/requests/original-rid/result", headers=env.api.headers)
    assert result.status_code == 200, result.text
    assert result.json()["text"] == "ORIGINAL-ANSWER\n\nAPPEND-APPLIED"


def start_original(env):
    response = env.api.client.post(env.submit_prefix + "/submit", headers=env.api.headers,
                                   json={"request_id": "original-rid", "content": "Original work."})
    assert (response.status_code == 202 or response.status_code == 503
            and response.json()["error"]["code"] == "NATIVE_RESPONSE_UNCONFIRMED"), response.text
    env.wait_model("original-rid")
    return env.read("original-rid")


def append_command(original, identifier, content="追加-保留原工作"):
    return {"request_id": original["request_id"], "conversation_id": original["conversation_id"],
            "append_id": identifier, "expected_submit_digest": original["submit_digest"], "content": content}


def read_append(env, command):
    response = env.api.client.get(
        PREFIX + "/conversations/" + command["conversation_id"] + "/requests/" + command["request_id"]
        + "/appends/" + command["append_id"], headers=env.api.headers)
    assert response.status_code == 200, response.text
    return response.json()


def wait_handoff(env, command):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = read_append(env, command)
        if value["sdk_message_id"] is not None:
            return value
        time.sleep(0.02)
    pytest.fail("The accepted append was not handed to the original SDK")


def test_owner_append_order_and_replay_do_not_create_another_work_or_send(append_sdk):
    env = append_sdk
    original = start_original(env)
    first = append_command(original, "append-a", "追加-保留原工作-A")
    second = append_command(original, "append-b", "追加-保留原工作-B")
    for command in (first, first, second, second):
        response = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command)
        assert response.status_code == 202, response.text
    conflict = env.api.client.post(PREFIX + "/append", headers=env.api.headers,
                                   json={**first, "content": "Changed original append"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "NATIVE_RID_CONFLICT"
    a, b = wait_handoff(env, first), wait_handoff(env, second)
    assert (a["sequence"], b["sequence"]) == (1, 2) and a["sdk_message_id"] != b["sdk_message_id"]
    env.hosted.portal.call(env.release.set)
    env.wait("original-rid", {"completed"})
    # Official queueing: each append is its own SDK round after the original work, in its original order.
    first_round, second_round = (json.dumps(body, ensure_ascii=False) for body in env.requests[1:])
    assert len(env.requests) == 3
    assert first_round.count(first["content"]) == 1 and second["content"] not in first_round
    assert second_round.count(first["content"]) == second_round.count(second["content"]) == 1
    assert second_round.index(first["content"]) < second_round.index(second["content"])
    assert read_append(env, first)["status"] == read_append(env, second)["status"] == "applied"
    saved = env.api.client.get(
        PREFIX + "/conversations/" + original["conversation_id"] + "/requests/original-rid/appends",
        headers=env.api.headers)
    assert saved.status_code == 200, saved.text
    assert [(entry["append_id"], entry["content"], entry["status"]) for entry in saved.json()["appends"]] == [
        ("append-a", first["content"], "applied"), ("append-b", second["content"], "applied")]
    assert env.read("original-rid")["command"] == original["command"] and env.api.c.control.records == {}
    assert env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=first).json()["status"] == "applied"
    assert len(env.requests) == 3


@pytest.mark.parametrize("failure", ["lost_ack", "invalid_ack"])
def test_unknown_sdk_append_ack_is_never_retried_or_reported_not_applied(append_sdk, monkeypatch, failure):
    from copilot.session import CopilotSession

    env = append_sdk
    original = start_original(env)
    send = CopilotSession.send
    sent = []

    async def uncertain(session, prompt, **options):
        result = await send(session, prompt, **options)
        if prompt == "追加-保留原工作":
            sent.append(result)
            if failure == "lost_ack":
                raise RuntimeError("OFFLINE SDK accepted the text but the ACK was lost")
            return None
        return result

    monkeypatch.setattr(CopilotSession, "send", uncertain)
    command = append_command(original, "uncertain-append")
    assert env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command).status_code == 202
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not sent:
        time.sleep(0.02)
    assert len(sent) == 1
    env.hosted.portal.call(env.release.set)
    env.wait("original-rid", {"unknown", "completed"})
    receipt = read_append(env, command)
    assert receipt["status"] == "unknown" and receipt["reason"] == "sdk_unconfirmed", receipt
    replay = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command)
    assert replay.status_code == 202 and replay.json()["status"] == "unknown", replay.text
    assert len(sent) == 1


def test_lost_owner_ack_reads_the_saved_append_and_never_sends_another_sdk_message(append_sdk):
    env = append_sdk
    original = start_original(env)
    command = append_command(original, "lost-owner-ack")
    env.transport.lose_append_ack = True
    response = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command)
    assert response.status_code == 503 and env.transport.append_calls == 1, response.text
    saved = wait_handoff(env, command)
    assert saved["append_id"] == command["append_id"] and saved["sequence"] == 1
    assert env.transport.append_calls == 1
    env.hosted.portal.call(env.release.set)
    env.wait("original-rid", {"completed"})
    assert read_append(env, command)["status"] == "applied"
    assert len(env.requests) == 2
    assert json.dumps(env.requests[1], ensure_ascii=False).count(command["content"]) == 1


def test_append_target_and_version_are_fixed_and_approval_fields_are_not_controls(append_sdk, signing_key):
    env = append_sdk
    original = start_original(env)
    command = append_command(original, "must-not-change-target")
    for changes, status in (
        ({"conversation_id": "another-cid"}, 409),
        ({"expected_submit_digest": "f" * 64}, 409),
        ({"approved": True}, 422),
    ):
        rejected = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json={**command, **changes})
        assert rejected.status_code == status, rejected.text
    agent = {"Authorization": "Bearer " + token(signing_key, {
        "oid": env.api.c.control.policy.template.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))}
    assert env.api.client.post(PREFIX + "/append", headers=agent, json=command).status_code == 403
    assert env.transport.append_calls == 0
    assert env.read("original-rid")["command"] == original["command"]
    env.hosted.portal.call(env.release.set)
    env.wait("original-rid", {"completed"})
    late = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command)
    assert late.status_code == 202 and late.json()["status"] == "not_applied"
    assert late.json()["reason"] == "turn_finished" and late.json()["sdk_message_id"] is None
    assert len(env.requests) == 1


def test_cancel_is_accepted_while_model_and_append_are_waiting_without_a_model_slot(append_sdk):
    env = append_sdk
    original = start_original(env)
    command = append_command(original, "before-cancel")
    assert env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command).status_code == 202
    cancel = env.api.client.post(PREFIX + "/cancel", headers=env.api.headers, json={
        "operation": "cancel", "request_id": original["request_id"], "conversation_id": original["conversation_id"],
    })
    assert cancel.status_code == 200 and cancel.json()["cancel_requested"], cancel.text
    assert not env.release.is_set() and len(env.requests) == 1
    cancelled = env.wait("original-rid", {"unknown"})
    assert cancelled["failure_code"] == "NATIVE_TURN_CANCELLED"
    assert read_append(env, command)["status"] in {"unknown", "not_applied"}
    assert len(env.requests) == 1


@pytest.mark.parametrize("append_sdk", ["responses"], indirect=True)
def test_responses_turn_uses_the_same_owner_append_without_another_platform_start(append_sdk):
    env = append_sdk
    original = start_original(env)
    assert original["state_profile"] == "foundry-responses-v1" and original["response_id"]
    command = append_command(original, "responses-append")
    appended = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command)
    assert appended.status_code == 202, appended.text
    wait_handoff(env, command)
    env.hosted.portal.call(env.release.set)
    final = env.wait("original-rid", {"completed"})
    assert final["response_id"] == original["response_id"] and final["turn_number"] == 1
    assert read_append(env, command)["status"] == "applied"
    assert len(env.requests) == 2 and "追加-保留原工作" in json.dumps(env.requests[1], ensure_ascii=False)


def test_append_admission_leaves_room_for_its_future_sdk_receipt_at_the_real_store_limit(append_sdk):
    env = append_sdk
    original = start_original(env)
    first = append_command(original, "first")
    assert env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=first).status_code == 202
    wait_handoff(env, first)
    command = append_command(original, "needs-receipt-room")

    async def retained_full_history():
        async with FoundryStateStore("fa/v1/turns") as store:
            key = "appends:" + hashlib.sha256(original["request_id"].encode()).hexdigest()
            item = await store.get_item(key)
            value = copy.deepcopy(item.value)
            entry = {"command": {"operation": "append", **command}, "status": "accepted",
                     "send_token": None, "sdk_message_id": None, "delivery": None, "reason": None}

            def size(body):
                return len(json.dumps(body, allow_nan=False).encode("utf-8"))

            with_candidate = {**value, "entries": [*value["entries"], entry]}
            target = 1_000_000 - (size(with_candidate) - size(value)) - 16
            index = 0
            while True:
                index += 1
                previous = {**entry, "command": {**entry["command"], "append_id": f"prior-{index}", "content": "x" * 7000},
                            "status": "applied", "send_token": f"prior-send-{index}",
                            "sdk_message_id": f"prior-sdk-{index}", "delivery": "steering"}
                candidate = {**value, "entries": [*value["entries"], previous]}
                if size(candidate) <= target:
                    value = candidate
                    continue
                previous["command"]["content"] = "x"
                short = {**value, "entries": [*value["entries"], previous]}
                if size(short) <= target:
                    previous["command"]["content"] += "x" * (target - size(short))
                    value = {**value, "entries": [*value["entries"], previous]}
                else:
                    value["entries"][-1]["command"]["content"] += "x" * (target - size(value))
                break
            assert size(value) == target
            assert size({**value, "entries": [*value["entries"], entry]}) == 999_984
            await store.set_item(key, value, if_match=item.etag)

    env.hosted.portal.call(retained_full_history)
    refused = env.api.client.post(PREFIX + "/append", headers=env.api.headers, json=command)
    assert refused.status_code == 413, refused.text
    assert len(env.requests) == 1
    env.hosted.portal.call(env.release.set)
    env.wait("original-rid", {"completed"})
    assert read_append(env, first)["status"] == "applied"
