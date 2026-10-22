"""Owner API and shared two-step transport, with only platform/Hosted HTTP replaced."""

import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_native_turn_wire import (
    NATIVE_REQUEST, NativeAgentCommand, NativeAgentReceive, NativeCancel, NativeConversation,
    NativeReceipt, NativeResponseNotStarted, NativeSubmit,
)
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NativeResponseDispatch
from shared_context.errors import Problem
from test_agent_versions import Foundry, REAL_CURRENT
from test_native_turn_api import native_api, PREFIX as NATIVE_PREFIX
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key, token


def same_original(posts):
    """Every send of one RID carries the same original material. Only the owner's after-reply send streams
    (RTT B preview); reconcile and waiting callers keep ``stream: false`` so a platform lock stays a 409."""
    bodies = [json.loads(call.content) for call in posts]
    material = [{key: value for key, value in body.items() if key != "stream"} for body in bodies]
    streams = [body["stream"] for body in bodies]
    return (streams.count(True) == 1 and streams.count(False) == len(streams) - 1
            and all(value == material[0] for value in material)
            and len({call.headers["x-client-fa-rid"] for call in posts}) == 1)


PREFIX = "/work-policy/v1/responses-turns"


def choose_responses(case, signing_key):
    path = "/work-policy/v1/agent-template"
    template = case.client.get(path, headers=case.headers)
    assert template.status_code == 200, template.text
    value = template.json()
    value["settings"]["state_profile"] = NATIVE_RESPONSES_PROFILE
    selected = case.client.post(path + "/select",
                               headers=case.c.h.headers(signing_key, "Behavior.Read Behavior.Approve"), json={
        "request_id": "responses-selection", "expected_selection_id": None, "template": value,
    })
    assert selected.status_code == 200, selected.text


@pytest.fixture
def responses_api(native_api, monkeypatch, signing_key):
    from shared_context import behavior_native_responses_api

    case = native_api
    monkeypatch.setattr(behavior_native_responses_api, "current_hosted_version",
                        lambda runtime, target, **_: case.remote.target.agent_version)
    monkeypatch.setattr("shared_context.behavior_agent_versions.current_hosted_version",
                        lambda runtime, target, **_: case.remote.target.agent_version)
    choose_responses(case, signing_key)
    calls, conversations, state = [], {}, {
        "queued": False, "response_failure": None, "holder": None, "history_items": [], "history_metadata": None,
        "response_snapshot": {}, "response_status": "queued", "response_dispatch_after": "already_queued",
    }

    def reply(value, status=200):
        return httpx.Response(status, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": case.remote.target.session_id,
            "x-agent-invocation-id": "offline-control",
        })

    def transport(request):
        calls.append(request)
        if "/protocols/openai/" in request.url.path:
            body = json.loads(request.content) if request.content else None
            if request.url.path.endswith("/conversations") and request.method == "POST":
                cid = "conv-server-" + str(len(conversations) + 1)
                conversations[cid] = {"object": "conversation", "id": cid, "metadata": body["metadata"]}
                return httpx.Response(200, json=conversations[cid])
            if request.url.path.endswith("/responses") and request.method == "POST":
                failure = state["response_failure"]
                if failure == "timeout":
                    raise httpx.ReadTimeout("OFFLINE lost ACK", request=request)
                if failure is not None:
                    return httpx.Response(failure, json={"error": {"code": "conversation_locked" if failure == 409
                                                                  else "unconfirmed", "message": "PRIVATE"}})
                if state["response_dispatch_after"] is not None:
                    rid = request.headers["x-client-fa-rid"]
                    original = case.remote.records[rid]
                    case.remote.records[rid] = original.model_copy(update={
                        "dispatch": NativeResponseDispatch(
                            state=state["response_dispatch_after"], inflight_request_id=rid)})
                return httpx.Response(200, json={
                    "object": "response", "id": "resp-platform", "status": state["response_status"],
                    "agent_session_id": case.remote.target.session_id,
                })
            if request.url.path.endswith("/cancel"):
                return httpx.Response(200, json={"object": "response", "id": "resp-original", "status": "cancelled"})
            if request.method == "GET" and request.url.path.endswith(("/items", "/input_items")):
                entries = state["history_items"]
                return httpx.Response(200, json={"object": "list", "data": entries, "has_more": False,
                    "first_id": entries[0]["id"] if entries else None, "last_id": entries[-1]["id"] if entries else None})
            if request.method == "GET" and "/responses/" in request.url.path:
                return httpx.Response(200, json={
                    "object": "response", "id": request.url.path.rsplit("/", 1)[-1], "status": "completed",
                    "metadata": {"fa_agent_id": case.remote.target.agent_id,
                                 "fa_team_id": case.c.control.policy.template.team_id},
                    "output": [], **state["response_snapshot"],
                })
            if request.method == "GET" and "/conversations/" in request.url.path:
                cid = request.url.path.split("/conversations/", 1)[1].split("/", 1)[0]
                metadata = state["history_metadata"]
                return httpx.Response(200, json={"object": "conversation", "id": cid,
                    "metadata": ({"fa_agent_id": case.remote.target.agent_id,
                                  "fa_team_id": case.c.control.policy.template.team_id} if metadata is None else metadata)})
            raise AssertionError("Unexpected platform operation")
        command = NATIVE_REQUEST.validate_json(request.content)
        if isinstance(command, (NativeSubmit, NativeAgentReceive)) and command.state_profile == NATIVE_RESPONSES_PROFILE:
            cid = command.command.conversation_id or command.new_conversation_id
            holder = state["holder"]
            busy = holder is not None and holder != command.command.request_id
            status = "busy_refused" if busy and isinstance(command, NativeSubmit) else "accepted"
            receipt = case.remote.records.get(command.command.request_id)
            if receipt is None:
                receipt = NativeReceipt(
                    state_profile=NATIVE_RESPONSES_PROFILE, agent_id=case.remote.target.agent_id,
                    hosted_session_id=case.remote.target.session_id, conversation_id=cid,
                    request_id=command.command.request_id, command=command.command, capture=command.capture,
                    submit_digest=command.sha256, capture_digest=command.capture.sha256, status=status,
                    source=getattr(command, "source", None),
                    failure_code="NATIVE_TURN_BUSY" if status == "busy_refused" else None,
                    dispatch=(NativeResponseDispatch(state="not_taken" if busy else
                                                      "already_queued" if state["queued"] else "needs_response",
                                                      inflight_request_id=holder if busy else command.command.request_id)
                              if status == "accepted" else None),
                )
                case.remote.records[receipt.request_id] = receipt
            return reply(receipt, 202)
        if command.operation == "conversation":
            original = next(value for value in case.remote.records.values()
                            if value.conversation_id == command.conversation_id)
            if original.state_profile == NATIVE_RESPONSES_PROFILE:
                return reply(NativeConversation(
                    state_profile=original.state_profile, agent_id=original.agent_id,
                    hosted_session_id=original.hosted_session_id, execution_hosted_session_id=original.hosted_session_id,
                    conversation_id=original.conversation_id, capture=original.capture,
                    capture_digest=original.capture_digest, last_turn=0, last_request_id=None,
                    inflight_request_id=state["holder"] or original.request_id,
                ))
        if isinstance(command, NativeResponseNotStarted):
            raise AssertionError("Unproven HTTP refusal must not close the original accepted receipt")
        if isinstance(command, NativeCancel):
            original = case.remote.records[command.request_id]
            if original.state_profile == NATIVE_RESPONSES_PROFILE:
                if original.status == "accepted":
                    original = original.model_copy(update={
                        "status": "unknown", "failure_code": "NATIVE_TURN_CANCELLED",
                        "cancel_requested": True, "dispatch": None})
                elif original.status == "started":
                    original = original.model_copy(update={"cancel_requested": True})
                case.remote.records[command.request_id] = original
                return reply(original)
        return case.remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(transport))
    yield SimpleNamespace(case=case, calls=calls, conversations=conversations, state=state)


def test_owner_submission_admits_original_capture_then_sends_one_stored_background_response(responses_api, monkeypatch):
    from starlette.background import BackgroundTasks

    env, command = responses_api, {"request_id": "responses-rid", "content": "原始問題"}
    replied, run = [], BackgroundTasks.__call__

    async def after_reply(self):
        replied.append(len(env.calls))
        await run(self)

    monkeypatch.setattr(BackgroundTasks, "__call__", after_reply)
    response = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json=command)
    assert response.status_code == 202, response.text
    original = NativeReceipt.model_validate_json(response.content)
    assert original.state_profile == NATIVE_RESPONSES_PROFILE and original.status == "accepted"
    assert original.dispatch.state == "needs_response"
    assert original.command.conversation_id is None and "conversation_id" not in original.command.model_dump(mode="json")
    assert original.conversation_id == "conv-server-1" and original.response_id is None
    invocations = [json.loads(call.content) for call in env.calls if call.url.path.endswith("/invocations")]
    admission = next(value for value in invocations if value["operation"] == "submit")
    assert admission["state_profile"] == NATIVE_RESPONSES_PROFILE
    assert admission["new_conversation_id"] == original.conversation_id
    posted = [call for call in env.calls if call.method == "POST" and call.url.path.endswith("/responses")]
    assert len(posted) == 1
    # Hosted's accepted receipt is the reply; the one POST follows it and nothing re-reads the receipt.
    assert replied and env.calls[replied[0]:] == posted
    assert posted[0].headers["x-client-fa-expected-version"] == env.case.remote.target.agent_version
    assert all(call.headers["x-client-fa-expected-version"] == env.case.remote.target.agent_version
               for call in env.calls if call.url.path.endswith("/invocations"))
    assert posted[0].headers["x-client-fa-submit-digest"] == original.submit_digest
    assert posted[0].headers["x-client-fa-rid"] == original.request_id
    assert json.loads(posted[0].content)["conversation"] == original.conversation_id
    # RTT B: only this owner send is created as a replayable stream; its first event is the acknowledgment.
    assert json.loads(posted[0].content)["stream"] is True and posted[0].headers["accept"] == "text/event-stream"
    assert env.case.c.control.records == {}


def test_responses_route_cannot_choose_the_lane_of_an_unselected_template(native_api):
    result = native_api.client.post(PREFIX + "/submit", headers=native_api.headers,
                                    json={"request_id": "cannot-select-lane", "content": "original"})
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_STATE_SCOPE", result.text
    assert native_api.remote.records == {}


@pytest.mark.parametrize("failure", ["timeout", 409, 400, 404, 502])
def test_unknown_second_step_keeps_the_original_receipt_and_reconciles_the_same_rid(responses_api, failure):
    env = responses_api
    env.state["response_failure"] = failure
    command = {"request_id": "same-rid", "content": "Keep this original text"}
    first = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json=command)
    # The POST runs after the reply, so its outcome is never part of the submit result.
    assert first.status_code == 202 and first.json()["dispatch"]["state"] == "needs_response", first.text
    observed = env.case.client.get(NATIVE_PREFIX + "/requests/same-rid", headers=env.case.headers)
    assert observed.status_code == 200 and observed.json()["status"] == "accepted", observed.text
    original = observed.json()
    env.state["response_failure"] = None
    reconciled = env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers, json=command)
    assert reconciled.status_code == 202, reconciled.text
    assert reconciled.json() == {**original, "dispatch": {**original["dispatch"], "state": "already_queued"}}
    posts = [call for call in env.calls if call.method == "POST" and call.url.path.endswith("/responses")]
    assert len(posts) == 2 and same_original(posts)
    assert posts[0].headers["x-client-fa-rid"] == posts[1].headers["x-client-fa-rid"] == command["request_id"]
    assert len(env.conversations) == 1


@pytest.mark.parametrize("status", ["queued", "in_progress", "completed", "failed", "cancelled", "incomplete"])
def test_platform_ack_is_unconfirmed_while_the_original_turn_still_needs_dispatch(responses_api, status):
    env = responses_api
    env.state["response_status"], env.state["response_dispatch_after"] = status, None
    command = {"request_id": "failed-ack", "content": "Keep the original intent"}
    submitted = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json=command)
    assert submitted.status_code == 202 and submitted.json()["dispatch"]["state"] == "needs_response"
    for _ in range(2):
        response = env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers, json=command)
        assert response.status_code == 503 and response.json()["error"]["code"] == "NATIVE_RESPONSE_UNCONFIRMED"
        assert response.json()["error"]["reconcile_original"] is True
    observed = env.case.client.get(NATIVE_PREFIX + "/requests/failed-ack", headers=env.case.headers)
    assert observed.status_code == 200 and observed.json()["status"] == "accepted"
    assert observed.json()["dispatch"]["state"] == "needs_response" and "response_id" not in observed.json()
    posts = [call for call in env.calls if call.method == "POST" and call.url.path.endswith("/responses")]
    assert len(posts) == 3 and same_original(posts)
    assert len(env.conversations) == 1


@pytest.mark.parametrize("dispatch", ["already_queued", "not_taken"])
def test_failed_ack_keeps_an_explicit_queue_or_slot_wait_outside_the_error_budget(responses_api, dispatch):
    env = responses_api
    env.state["response_failure"] = 502
    command = {"request_id": "known-wait", "content": "Original"}
    assert env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json=command).status_code == 202
    env.state.update(response_failure=None, response_status="failed", response_dispatch_after=dispatch)
    response = env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers, json=command)
    assert response.status_code == 202 and response.json()["status"] == "accepted"
    assert response.json()["dispatch"]["state"] == dispatch and "response_id" not in response.json()


def test_already_queued_reconcile_does_not_send_another_response(responses_api):
    env = responses_api
    command = {"request_id": "queued-rid", "content": "Original"}
    first = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json=command)
    assert first.status_code == 202
    original = env.case.remote.records[command["request_id"]]
    queued = original.model_copy(update={"dispatch": NativeResponseDispatch(
        state="already_queued", inflight_request_id=original.request_id)})
    env.case.remote.records[queued.request_id] = queued
    before = len(env.calls)
    read = env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers, json=command)
    assert read.status_code == 200 and read.json()["dispatch"]["state"] == "already_queued"
    assert not any(call.url.path.endswith("/responses") for call in env.calls[before:])


def test_reconcile_racing_the_after_reply_post_is_refused_by_the_platform_conversation_lock(responses_api, monkeypatch):
    env = responses_api
    command = {"request_id": "in-flight-rid", "content": "Original"}
    platform, during, running = env.case.foundry.http._transport, [], []

    def first_post_meets_a_reconcile(request):
        if request.method == "POST" and request.url.path.endswith("/responses"):
            if running:
                # SDK 2.2.0 with steerable_conversations=False: a second start on a running CID task is 409.
                env.calls.append(request)
                return httpx.Response(409, json={"error": {"code": "conversation_locked", "type": "conflict"}})
            if not during:
                running.append(True)
                during.append(env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers, json=command))
                try:
                    return platform.handle_request(request)
                finally:
                    running.clear()
        return platform.handle_request(request)

    monkeypatch.setattr(env.case.foundry.http, "_transport", httpx.MockTransport(first_post_meets_a_reconcile))
    submitted = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json=command)
    assert submitted.status_code == 202, submitted.text
    # The locked second POST is a pending wait for the same RID: no 503, no error budget, no new ID.
    assert during[0].status_code == 202 and during[0].json()["dispatch"]["state"] == "needs_response", during[0].text
    posts = [call for call in env.calls if call.method == "POST" and call.url.path.endswith("/responses")]
    assert len(posts) == 2 and same_original(posts)
    assert all(call.headers["x-client-fa-rid"] == command["request_id"] for call in posts)
    later = env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers, json=command)
    assert later.status_code == 200 and later.json()["dispatch"]["state"] == "already_queued", later.text
    assert sum(call.method == "POST" and call.url.path.endswith("/responses") for call in env.calls) == 2


def test_busy_new_request_advances_only_the_original_unstarted_holder(responses_api):
    env = responses_api
    env.state["response_failure"] = 409
    original = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                   json={"request_id": "holder-rid", "content": "Original"})
    assert original.status_code == 202
    env.state["response_failure"] = None
    env.state["holder"] = "holder-rid"
    busy = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json={
        "request_id": "later-rid", "conversation_id": original.json()["conversation_id"], "content": "Later request",
    })
    assert busy.status_code == 202 and busy.json()["status"] == "busy_refused", busy.text
    posts = [call for call in env.calls if call.method == "POST" and call.url.path.endswith("/responses")]
    assert len(posts) == 2
    assert all(call.headers["x-client-fa-rid"] == "holder-rid" for call in posts)
    assert same_original(posts)
    assert env.case.remote.records["holder-rid"].status == "accepted"


def test_holder_dispatch_uncertainty_reaches_the_waiting_callers_finite_budget(responses_api):
    from shared_context.behavior_native_responses_api import submit_native_turn
    from test_native_turn_wire import agent_receive

    env = responses_api
    env.state["response_failure"] = 409
    first = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                json={"request_id": "holder-rid", "content": "Original"})
    assert first.status_code == 202
    original = env.case.remote.records["holder-rid"]
    env.state.update(holder=original.request_id, response_failure=None, response_status="in_progress",
                     response_dispatch_after=None)
    previous = env.case.client.get(NATIVE_PREFIX + "/conversations/" + original.conversation_id,
                                   headers=env.case.headers)
    assert previous.status_code == 200
    conversation = NativeConversation.model_validate_json(previous.content)
    source = agent_receive("waiting-peer").source.model_copy(update={
        "recipient_agent_id": env.case.remote.target.agent_id})
    command = NativeAgentCommand(request_id="waiting-peer", conversation_id=original.conversation_id,
                                 content="Waiting for the original slot", model=original.capture.model.id)
    client = env.case.foundry.for_target(env.case.remote.target)
    accepted, _ = submit_native_turn(
        env.case.composition, client, command, original.capture, source=source, conversation=conversation,
        before_send=lambda: None)
    assert accepted.status == "accepted" and accepted.dispatch.state == "not_taken"
    for _ in range(2):
        with pytest.raises(Problem) as unconfirmed:
            submit_native_turn(env.case.composition, client, command, original.capture, source=source,
                               original=accepted, before_send=lambda: None)
        assert unconfirmed.value.status == 503
        assert unconfirmed.value.body["error"]["code"] == "NATIVE_RESPONSE_UNCONFIRMED"
    assert env.case.remote.records["waiting-peer"] == accepted
    assert env.case.remote.records["holder-rid"].status == "accepted"
    posts = [call for call in env.calls if call.method == "POST" and call.url.path.endswith("/responses")]
    assert len(posts) == 3 and all(call.headers["x-client-fa-rid"] == "holder-rid" for call in posts)
    assert same_original(posts)


def test_native_submit_cannot_take_a_responses_conversation_or_new_template(responses_api):
    env = responses_api
    first = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                json={"request_id": "responses-original", "content": "Original"})
    assert first.status_code == 202
    before = len(env.calls)
    for changes in ({}, {"conversation_id": first.json()["conversation_id"]}):
        refused = env.case.client.post(NATIVE_PREFIX + "/submit", headers=env.case.headers,
                                       json={"request_id": "wrong-route", "content": "Wrong", **changes})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_STATE_SCOPE", refused.text
    assert not any(call.url.path.endswith("/responses") for call in env.calls[before:])
    assert "wrong-route" not in env.case.remote.records


@pytest.mark.parametrize("started", [False, True], ids=["waiting-original-rid", "started-official-response"])
def test_cancel_retains_original_rid_and_uses_only_its_persisted_response_id(responses_api, started):
    env = responses_api
    initial = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                  json={"request_id": "cancel-original", "content": "Original"})
    assert initial.status_code == 202
    original = env.case.remote.records["cancel-original"]
    if started:
        original = original.model_copy(update={"status": "started", "response_id": "resp-original", "dispatch": None})
        env.case.remote.records[original.request_id] = original
    before = len(env.calls)
    result = env.case.client.post(PREFIX + "/cancel", headers=env.case.headers, json={
        "operation": "cancel", "request_id": original.request_id, "conversation_id": original.conversation_id,
    })
    assert result.status_code == 200 and result.json()["cancel_requested"] is True, result.text
    assert result.json()["status"] == ("started" if started else "unknown")
    cancels = [call for call in env.calls[before:] if call.url.path.endswith("/responses/resp-original/cancel")]
    assert len(cancels) == (1 if started else 0)
    assert not any(call.url.path.endswith("/responses") for call in env.calls[before:])
    assert result.json()["command"] == original.command.model_dump(mode="json")


def test_platform_history_is_scope_checked_without_vm_and_never_promotes_commentary_to_answer(responses_api):
    env = responses_api
    env.state["history_items"] = [
        {"id": "msg-user", "type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "原問題"}]},
        {"id": "msg-progress", "type": "message", "role": "assistant", "phase": "commentary",
         "content": [{"type": "output_text", "text": "PRIVATE-not-the-answer"}]},
        {"id": "reasoning", "type": "reasoning", "summary": [{"text": "PRIVATE-reasoning"}]},
        {"id": "msg-final", "type": "message", "role": "assistant", "phase": "final_answer", "status": "completed",
         "content": [{"type": "output_text", "text": "原答案"}]},
        {"id": "msg-no-phase", "type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "PRIVATE-unconfirmed-phase"}]},
    ]
    response = env.case.client.get(PREFIX + "/conversations/conv-history/history", headers=env.case.headers)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["source_kind"] == "conversation" and page["source_id"] == "conv-history"
    assert [(item["item_id"], item["text"], item["state"]) for item in page["messages"]] == [
        ("msg-user", "原問題", "available"), ("msg-final", "原答案", "available"),
        ("msg-no-phase", None, "unconfirmed"),
    ]
    assert page["fallback"] == "native-result" and page["next_after"] is None
    assert "PRIVATE" not in response.text and "request_id" not in response.text
    assert len(env.calls) == 2 and all(call.method == "GET" and "/protocols/openai/" in call.url.path for call in env.calls)


def test_platform_history_never_falls_back_to_vm_when_scope_metadata_is_missing(responses_api):
    env = responses_api
    env.state["history_metadata"] = {}
    response = env.case.client.get(PREFIX + "/conversations/conv-history/history", headers=env.case.headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "NATIVE_STATE_SCOPE", response.text
    assert len(env.calls) == 1 and env.calls[0].url.path.endswith("/conversations/conv-history")


@pytest.mark.parametrize("status,expected_text", [("completed", "Original final answer"), ("failed", None)])
def test_response_chain_history_preserves_the_link_without_claiming_a_fa_rid(responses_api, status, expected_text):
    env = responses_api
    env.state["history_items"] = [
        {"id": "msg-input", "type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "Original input"}]},
    ]
    env.state["response_snapshot"] = {
        "status": status, "previous_response_id": "resp-previous",
        "output": [{"id": "msg-final", "type": "message", "role": "assistant", "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "Original final answer"}]}],
    }
    result = env.case.client.get(PREFIX + "/responses/resp-chain/history", headers=env.case.headers)
    assert result.status_code == 200, result.text
    page = result.json()
    assert page["source_kind"] == "response" and page["source_id"] == "resp-chain"
    assert page["previous_response_id"] == "resp-previous" and page["next_after"] is None
    assert page["messages"][-1]["text"] == expected_text
    assert page["fallback"] == (None if status == "completed" else "native-result")
    assert "request_id" not in result.text and len(env.calls) == 2
    assert all(call.method == "GET" and "/protocols/openai/" in call.url.path for call in env.calls)


def test_refused_after_reply_post_uses_the_admitted_client_and_forgets_the_cached_reader(responses_api, monkeypatch):
    from time import monotonic

    from starlette.background import BackgroundTasks
    from shared_context.behavior_foundry_responses import FoundryResponsesClient
    from shared_context.behavior_native_sessions import native_sessions

    env = responses_api
    sessions = native_sessions(env.case.composition)
    marks, targets, cached, run = [], [], [], BackgroundTasks.__call__

    async def after_reply(self):
        cached.append(sessions.reader_cached())
        # A reader resolved again from here would name this other session, not the admission's.
        scope, target, _seen = sessions._reader
        sessions._reader = (scope, target.model_copy(update={"session_id": "fa-native-moved-elsewhere"}), monotonic())
        marks.append(len(env.calls))
        await run(self)
        cached.append(sessions.reader_cached())

    def refused_start(self, original, input_text, **_):
        targets.append((self.native.target.session_id, self.native.target.agent_version, original.request_id))
        raise Problem(503, "FOUNDRY_CREDENTIAL_UNAVAILABLE", "OFFLINE credential refused after the reply")

    monkeypatch.setattr(BackgroundTasks, "__call__", after_reply)
    monkeypatch.setattr(FoundryResponsesClient, "start", refused_start)
    submitted = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                    json={"request_id": "refused-after-reply", "content": "Original"})
    assert submitted.status_code == 202 and submitted.json()["dispatch"]["state"] == "needs_response", submitted.text
    # The send ran on the admission's own client: no session read after the reply, and not the swapped cached reader.
    assert env.calls[marks[0]:] == []
    assert targets == [(env.case.remote.target.session_id, env.case.remote.target.agent_version, "refused-after-reply")]
    # As a refused synchronous send did, the refusal reaches sessions.moved and the cached reader is forgotten.
    assert cached == [True, False]
    assert env.case.remote.records["refused-after-reply"].status == "accepted"


def test_platform_denial_keeps_accepted_and_never_closes_or_retries(responses_api):
    env = responses_api
    env.state["response_failure"] = 403
    command = {"request_id": "denied-second-step", "content": "Original"}
    submitted = env.case.client.post(PREFIX + "/submit", headers=env.case.headers, json=command)
    assert submitted.status_code == 202 and submitted.json()["dispatch"]["state"] == "needs_response", submitted.text
    denied = env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers, json=command)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "NATIVE_TURNS_DISABLED", denied.text
    retained = env.case.remote.records["denied-second-step"]
    assert retained.status == "accepted" and retained.failure_code is None and retained.response_id is None
    assert sum(call.method == "POST" and call.url.path.endswith("/responses") for call in env.calls) == 2


def test_undecided_routing_mode_never_creates_a_platform_conversation_or_admits_a_new_rid(responses_api, monkeypatch):
    from shared_context import behavior_native_responses_api

    env = responses_api
    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", None)
    blocked = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                  json={"request_id": "not-enabled", "content": "Original"})
    assert blocked.status_code == 503 and blocked.json()["error"]["code"] == "NATIVE_TURNS_DISABLED", blocked.text
    assert env.conversations == {} and "not-enabled" not in env.case.remote.records
    assert all("/protocols/openai/" not in call.url.path for call in env.calls)


def test_agent_producer_uses_the_same_admission_and_second_step_without_waiting_for_a_result(responses_api):
    from shared_context.behavior_native_responses_api import submit_native_turn
    from test_native_turn_wire import agent_receive

    env = responses_api
    initial = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                  json={"request_id": "owner-capture", "content": "Original"})
    assert initial.status_code == 202
    capture = env.case.remote.records["owner-capture"].capture
    message = agent_receive("peer-rid")
    source = message.source.model_copy(update={"recipient_agent_id": env.case.remote.target.agent_id})
    command = NativeAgentCommand(request_id="peer-rid", content="Original Agent request", model=capture.model.id)
    received, upstream = submit_native_turn(
        env.case.composition, env.case.foundry.for_target(env.case.remote.target), command, capture,
        source=source, before_send=lambda: None,
    )
    assert upstream.status_code == 202 and received.status == "accepted"
    assert received.source == source and received.command.authorize_own_result is False
    assert received.dispatch.state == "needs_response" and received.response_id is None
    assert received.conversation_id != initial.json()["conversation_id"]
    assert not any(call.url.path.endswith("/responses") and call.headers.get("x-client-fa-rid") == "peer-rid"
                   for call in env.calls)
    repeated, _ = submit_native_turn(
        env.case.composition, env.case.foundry.for_target(env.case.remote.target), command, capture,
        source=source, original=received, before_send=lambda: None,
    )
    assert repeated.model_dump(exclude={"dispatch"}) == received.model_dump(exclude={"dispatch"})
    assert repeated.dispatch.state == "already_queued" and len(env.conversations) == 2
    peers = [call for call in env.calls if call.url.path.endswith("/responses")
             and call.headers.get("x-client-fa-rid") == "peer-rid"]
    assert len(peers) == 1 and json.loads(peers[0].content)["input"] == command.content


def test_agent_second_step_error_is_not_reported_as_successful_slot_wait(responses_api):
    from shared_context.behavior_native_responses_api import submit_native_turn
    from test_native_turn_wire import agent_receive

    env = responses_api
    initial = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                  json={"request_id": "owner-capture", "content": "Original"})
    assert initial.status_code == 202
    capture = env.case.remote.records["owner-capture"].capture
    source = agent_receive("peer-rid").source.model_copy(update={"recipient_agent_id": env.case.remote.target.agent_id})
    command = NativeAgentCommand(request_id="peer-rid", content="Original Agent request", model=capture.model.id)
    env.state["response_failure"] = 502
    client = env.case.foundry.for_target(env.case.remote.target)
    received, upstream = submit_native_turn(
        env.case.composition, client, command, capture, source=source, before_send=lambda: None)
    assert received.status == "accepted" and upstream.status_code == 202
    with pytest.raises(Problem) as failed:
        submit_native_turn(env.case.composition, client, command, capture, source=source, original=received,
                           before_send=lambda: None)
    assert failed.value.status == 503 and failed.value.body["error"]["code"] == "NATIVE_RESPONSE_UNCONFIRMED"
    assert env.case.remote.records["peer-rid"].status == "accepted"
    assert len(env.conversations) == 2
    peers = [call for call in env.calls if call.url.path.endswith("/responses")
             and call.headers.get("x-client-fa-rid") == "peer-rid"]
    assert len(peers) == 1


def test_existing_invocations_cid_is_not_moved_by_a_new_responses_template(native_api, signing_key):
    case = native_api
    first = case.client.post(NATIVE_PREFIX + "/submit", headers=case.headers,
                            json={"request_id": "native-original", "content": "Old conversation"})
    assert first.status_code == 202
    original = case.remote.records["native-original"]
    choose_responses(case, signing_key)
    body = {"request_id": "wrong-responses-lane", "conversation_id": original.conversation_id, "content": "Follow-up"}
    refused = case.client.post(PREFIX + "/submit", headers=case.headers, json=body)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_STATE_SCOPE", refused.text
    continued = case.client.post(NATIVE_PREFIX + "/submit", headers=case.headers,
                                json={**body, "request_id": "native-follow"})
    # ADR-0009 2d-ii: the v1 Invocations conversation continues, sent (and so received) as v2
    assert continued.status_code == 202 and continued.json()["state_profile"] == "foundry-native-v2"
    assert case.remote.records["native-original"] == original


def test_responses_owner_routes_reject_agent_credentials_and_client_execution_fields(responses_api, signing_key):
    env = responses_api
    before = len(env.calls)
    for field in ("state_profile", "new_conversation_id", "response_id", "hosted_session_id", "submit_digest"):
        refused = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                      json={"request_id": "forged", "content": "Original", field: "client-selected"})
        assert refused.status_code == 422
    agent_headers = {"Authorization": "Bearer " + token(signing_key, {
        "oid": env.case.c.control.policy.template.agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",))}
    denied = env.case.client.post(PREFIX + "/submit", headers=agent_headers,
                                 json={"request_id": "agent-cannot-impersonate", "content": "Original"})
    assert denied.status_code == 403
    history_denied = env.case.client.get(PREFIX + "/conversations/conv-history/history", headers=agent_headers)
    assert history_denied.status_code == 403
    assert len(env.calls) == before


def test_reconcile_of_missing_rid_never_recreates_its_conversation(responses_api):
    env = responses_api
    result = env.case.client.post(PREFIX + "/reconcile", headers=env.case.headers,
                                 json={"request_id": "absent-original", "content": "Original"})
    assert result.status_code == 404 and result.json()["error"]["code"] == "NATIVE_RID_MISSING", result.text
    assert not env.conversations
    assert all("/protocols/openai/" not in call.url.path for call in env.calls)


def test_shared_producer_reads_terminal_original_without_a_new_transport_or_foreign_agent(responses_api, monkeypatch):
    from shared_context import behavior_native_responses_api

    env = responses_api
    initial = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                  json={"request_id": "original-rid", "content": "Original"})
    assert initial.status_code == 202
    original = env.case.remote.records["original-rid"].model_copy(update={
        "status": "unknown", "failure_code": "NATIVE_TURN_CANCELLED", "dispatch": None})
    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", None)
    before = len(env.calls)
    result, upstream = behavior_native_responses_api.submit_native_turn(
        env.case.composition, env.case.foundry.for_target(env.case.remote.target),
        original.command, original.capture, original=original,
        before_send=lambda: pytest.fail("A terminal original must not send another operation"),
    )
    assert result == original and upstream is None and len(env.calls) == before
    with pytest.raises(Problem) as denied:
        behavior_native_responses_api.submit_native_turn(
            env.case.composition, env.case.foundry.for_target(env.case.remote.target),
            original.command, original.capture, original=original.model_copy(update={"agent_id": "foreign-agent"}),
            before_send=lambda: pytest.fail("A foreign original must not send"),
        )
    assert denied.value.status == 403 and len(env.calls) == before


def test_shared_producer_requires_the_original_conversation_when_admitting_a_followup(responses_api):
    from shared_context.behavior_native_responses_api import submit_native_turn
    from shared_context.behavior_assistant_wire import InstructionSubmit

    env = responses_api
    first = env.case.client.post(PREFIX + "/submit", headers=env.case.headers,
                                json={"request_id": "first", "content": "Original"})
    assert first.status_code == 202
    original = env.case.remote.records["first"]
    before = len(env.calls)
    with pytest.raises(Problem) as missing:
        submit_native_turn(
            env.case.composition, env.case.foundry.for_target(env.case.remote.target),
            InstructionSubmit(request_id="follow", conversation_id=original.conversation_id, content="Follow"),
            original.capture, before_send=lambda: None,
        )
    assert missing.value.body["error"]["code"] == "NATIVE_CONVERSATION_MISSING"
    assert len(env.calls) == before and "follow" not in env.case.remote.records


@pytest.fixture
def options_api(native_api, monkeypatch):
    from conftest import InMemoryRepository

    monkeypatch.setattr(native_api.c.authority.artifacts, "repository", InMemoryRepository())
    return native_api


def test_template_options_publish_disabled_responses_without_platform_lookup(options_api, monkeypatch):
    from shared_context import behavior_native_responses_api

    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", None)
    monkeypatch.setattr(behavior_native_responses_api, "current_hosted_version",
                        lambda *_a, **_k: pytest.fail("Unselected routing must not query the platform"))
    response = options_api.client.get("/work-policy/v1/agent-template/options", headers=options_api.headers)
    assert response.status_code == 200, response.text
    assert response.json()["responses"] == {"enabled": False, "history_source": None}
    assert not options_api.remote.calls and not options_api.remote.records


@pytest.fixture
def options_version(options_api, monkeypatch):
    from shared_context import behavior_native_responses_api
    from shared_context.behavior_agent_versions import HostedVersions

    case = options_api
    target = case.foundry.target
    foundry = Foundry(target)
    foundry.add(target.agent_name, target.agent_version, "options-build", definition={
        "kind": "hosted", "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": {"FA_NATIVE_TURNS": "foundry-native-v2", "FA_RESPONSES_ENABLED": "1"},
    })
    monkeypatch.setattr(case.composition, "hosted_versions",
                        HostedVersions(lambda _: foundry, build="options-build"), raising=False)
    monkeypatch.setattr(behavior_native_responses_api, "current_hosted_version", REAL_CURRENT)
    return SimpleNamespace(api=case, foundry=foundry, target=target)


def test_production_modes_project_conversation_history_without_selecting_a_new_lane(options_version):
    from shared_context import behavior_native_responses_api, native_responses_wire
    from shared_context.behavior_agent_template import AgentTemplate

    assert behavior_native_responses_api.RESPONSES_CONVERSATION_MODE == "conversation"
    assert native_responses_wire.RESPONSES_ENVIRONMENT_MODE == "retain-cid"
    assert native_responses_wire.responses_environment_blocker() is None
    case = options_version.api
    response = case.client.get("/work-policy/v1/agent-template/options", headers=case.headers)
    assert response.status_code == 200, response.text
    assert response.json()["responses"] == {"enabled": True, "history_source": "conversation"}
    template = case.client.get("/work-policy/v1/agent-template", headers=case.headers)
    assert template.status_code == 200
    assert AgentTemplate.model_validate_json(template.content).settings.state_profile == "foundry-native-v1"
    assert not case.remote.calls and not case.remote.records


@pytest.mark.parametrize(("mode", "source"), [("conversation", "conversation"), ("previous_response_id", "response")])
def test_template_options_use_the_endpoint_selected_version_for_enabled_responses(options_version, monkeypatch, mode, source):
    from shared_context import behavior_native_responses_api

    case, foundry, target = options_version.api, options_version.foundry, options_version.target
    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", mode)
    for _ in range(2):
        response = case.client.get("/work-policy/v1/agent-template/options", headers=case.headers)
        assert response.status_code == 200, response.text
        assert response.json()["responses"] == {"enabled": True, "history_source": source}
        assert response.json()["authorization"] is False and response.json()["activated"] is False
    # The selector is read every time; the selected version's definition once per process.
    assert foundry.calls == [("agent", target.agent_name), ("get", target.agent_name, target.agent_version),
                             ("agent", target.agent_name)]
    assert not case.remote.calls and not case.remote.records


@pytest.mark.parametrize("disabled", ["api", "native", "responses", "protocol", "probe"])
def test_template_options_keep_history_source_when_new_responses_are_disabled(options_version, monkeypatch, disabled):
    case, foundry, target = options_version.api, options_version.foundry, options_version.target
    definition = foundry.versions[target.agent_name][0]["definition"]
    if disabled == "api":
        monkeypatch.delenv("FA_NATIVE_TURNS")
    elif disabled in {"native", "responses"}:
        definition["environment_variables"].pop("FA_NATIVE_TURNS" if disabled == "native" else "FA_RESPONSES_ENABLED")
    elif disabled == "protocol":
        definition["protocol_versions"] = [{"protocol": "invocations", "version": "2.0.0"}]
    else:
        definition["environment_variables"]["FA_N6_RESPONSES_PROBE"] = "1"
    response = case.client.get("/work-policy/v1/agent-template/options", headers=case.headers)
    if disabled == "native":  # ADR-0009 2d-ii: a version that declares no v2 is not followed at all
        assert (response.status_code, response.json()["error"]["code"]) == (503, "AGENT_VERSION_FOLLOWING"), response.text
        return
    assert response.status_code == 200, response.text
    assert response.json()["responses"] == {"enabled": False, "history_source": "conversation"}
    expected = [] if disabled == "api" else [("agent", target.agent_name), ("get", target.agent_name, target.agent_version)]
    assert foundry.calls == expected and not case.remote.calls and not case.remote.records


@pytest.mark.parametrize("status", [401, 403, None])
def test_template_options_surface_unreadable_responses_eligibility(options_version, status):
    from shared_context.behavior_agent_onboarding import FoundryCallError

    case, foundry = options_version.api, options_version.foundry
    def denied(_name):
        raise FoundryCallError(status)
    foundry.get_agent = denied
    response = case.client.get("/work-policy/v1/agent-template/options", headers=case.headers)
    assert response.status_code == (503 if status is None else status), response.text
    assert response.json()["error"]["code"] == ("AGENT_VERSION_FOLLOWING" if status is None else "NATIVE_TURNS_DISABLED")
    assert "responses" not in response.json() and not case.remote.calls and not case.remote.records


def test_template_options_do_not_read_responses_eligibility_after_scope_revocation(options_version):
    case, foundry = options_version.api, options_version.foundry
    case.c.control.revoke(case.c.human, "revoke-options")
    response = case.client.get("/work-policy/v1/agent-template/options", headers=case.headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "WORK_POLICY_REVOKED"
    assert foundry.calls == [] and not case.remote.calls
