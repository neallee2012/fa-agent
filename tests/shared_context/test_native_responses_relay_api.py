"""T5 producer uses the shared N6-2 admission/dispatch helper, not a second Responses client."""

from dataclasses import replace
import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_context.behavior_agent_template import AgentTemplate
from shared_context.behavior_native_agent_wire import NativeRelayResult
from shared_context.behavior_native_turn_wire import (
    NATIVE_REQUEST, NativeAgentReceive, NativeConversation, NativeConversationRead, NativeRead,
    NativeReceipt, NativeResult, NativeResultPage, NativeResultReference, NativeSubmit, NativeTurnCapture,
)
from shared_context.behavior_profile_selection import SelectConversationProfile, current_selection, select_profile
from shared_context.errors import Problem
from shared_context.native_agent_api import NativeAgentRelayGateway
from shared_context.native_agent_relay import NativePeerRequest, prepare_request
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NATIVE_STATE_PROFILE, NativeResponseDispatch
from test_native_agent_api import (
    instruction_models, jwk_client, peer_runtime, relay_case, send_command, signing_key, synthetic_standing_v1,
)
from test_native_agent_relay import capture, sender_case
from test_agent_runtime_routing import agent_headers


class ResponsesPeerTransport:
    """Only the platform and VM HTTP boundary, with the published 1a receipt shape."""

    def __init__(self, native):
        self.native, self.calls, self.queued = native, [], set()
        self.conversations = []
        self.failure = None

    def reply(self, value, *, status=200):
        return httpx.Response(status, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": self.native.target.session_id,
            "x-agent-invocation-id": "offline-original-invocation"})

    def projected(self, value):
        if value.state_profile == NATIVE_RESPONSES_PROFILE and value.status == "accepted":
            return value.model_copy(update={"dispatch": NativeResponseDispatch(
                state="already_queued" if value.request_id in self.queued else "needs_response",
                inflight_request_id=value.request_id)})
        return value

    def __call__(self, request):
        self.calls.append(request)
        if "/protocols/openai/" in request.url.path:
            body = json.loads(request.content) if request.content else None
            if request.url.path.endswith("/conversations"):
                cid = "conv-peer-" + str(len(self.conversations) + 1)
                self.conversations.append(cid)
                return httpx.Response(200, json={"object": "conversation", "id": cid, "metadata": body["metadata"]})
            assert request.url.path.endswith("/responses") and request.method == "POST"
            if self.failure == "timeout":
                raise httpx.ReadTimeout("OFFLINE original response ACK lost", request=request)
            if self.failure is not None:
                return httpx.Response(self.failure, json={"error": {
                    "code": "conversation_locked" if self.failure == 409 else "unconfirmed"}})
            rid = request.headers["x-client-fa-rid"]
            self.queued.add(rid)
            return httpx.Response(200, json={
                "object": "response", "id": "resp-peer", "status": "queued",
                "agent_session_id": self.native.target.session_id})
        command = NATIVE_REQUEST.validate_json(request.content)
        if isinstance(command, NativeRead) and command.request_id in self.native.records:
            return self.reply(self.projected(self.native.records[command.request_id]))
        if isinstance(command, NativeConversationRead):
            original = next(value for value in self.native.records.values()
                            if value.conversation_id == command.conversation_id)
            return self.reply(NativeConversation(
                state_profile=original.state_profile, agent_id=original.agent_id,
                hosted_session_id=original.hosted_session_id, execution_hosted_session_id=original.hosted_session_id,
                conversation_id=original.conversation_id, capture=original.capture, capture_digest=original.capture_digest,
                last_turn=original.turn_number or 0, last_request_id=original.request_id if original.turn_number else None))
        if isinstance(command, NativeResult):
            original = self.native.records[command.request_id]
            total = original.result.text_parts
            start = len(self.native.text) * command.part // total
            end = len(self.native.text) * (command.part + 1) // total
            return self.reply(NativeResultPage(
                conversation_id=original.conversation_id, request_id=original.request_id, result=original.result,
                part=command.part, text=self.native.text[start:end], complete=total == 1,
                next_part=command.part + 1 if command.part + 1 < total else None))
        if isinstance(command, (NativeAgentReceive, NativeSubmit)) and command.state_profile == NATIVE_RESPONSES_PROFILE:
            original = self.native.records.get(command.command.request_id)
            if original is not None and original.submit_digest != command.sha256:
                return httpx.Response(409, json={"error": {
                    "code": "NATIVE_RID_CONFLICT", "message": "The original RID has different admission material."}})
            value = original or NativeReceipt(
                state_profile=NATIVE_RESPONSES_PROFILE, agent_id=self.native.target.agent_id,
                hosted_session_id=self.native.target.session_id, request_id=command.command.request_id,
                conversation_id=command.command.conversation_id or command.new_conversation_id,
                command=command.command, capture=command.capture, source=getattr(command, "source", None),
                submit_digest=command.sha256, capture_digest=command.capture.sha256, status="accepted")
            self.native.records[value.request_id] = value
            if self.native.lose_submit:
                raise httpx.ReadTimeout("OFFLINE accepted control response lost", request=request)
            return self.reply(self.projected(value), status=202)
        return self.native(request)


@pytest.fixture
def responses_relay(relay_case, monkeypatch):
    from shared_context import behavior_native_responses_api

    case = relay_case
    control = case.base.b.runtime.work_policy
    selected = current_selection(control.store, control.policy)
    template = selected.command.template.model_dump(mode="json")
    template["settings"]["state_profile"] = NATIVE_RESPONSES_PROFILE
    select_profile(control, case.base.b.assistant.authority, case.base.c.human, SelectConversationProfile(
        request_id="select-response-peer", expected_selection_id=selected.selection_id,
        template=AgentTemplate.model_validate_json(json.dumps(template))))
    monkeypatch.setattr(behavior_native_responses_api, "RESPONSES_CONVERSATION_MODE", "conversation")
    monkeypatch.setattr(behavior_native_responses_api, "current_hosted_version",
                        lambda runtime, target, **_kwargs: target.agent_version)
    remotes = [ResponsesPeerTransport(remote) for remote in case.remotes]
    for client, remote in zip(case.clients, remotes):
        monkeypatch.setattr(client.http, "_transport", httpx.MockTransport(remote))
    return SimpleNamespace(case=case, remotes=remotes,
        gateway=NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository))


def test_t5_fresh_request_only_accepts_then_same_message_uses_shared_response_dispatch(responses_relay):
    env, command = responses_relay, send_command(responses_relay.case)
    first = env.gateway.send(env.case.principal, command)
    assert first.state_profile == NATIVE_RESPONSES_PROFILE
    assert first.status == "accepted" and first.dispatch.state == "needs_response" and first.response_id is None
    peer = env.remotes[1]
    assert not any(call.url.path.endswith("/responses") for call in peer.calls)
    original = env.case.remotes[1].records[first.message_id]
    assert original.command.conversation_id is None
    second = env.gateway.send(env.case.principal, command)
    assert second.message_id == first.message_id and second.conversation_id == first.conversation_id
    assert second.submit_digest == first.submit_digest and second.dispatch.state == "already_queued"
    assert len([call for call in peer.calls if call.url.path.endswith("/responses")]) == 1
    assert env.gateway.send(env.case.principal, command) == second
    assert len([call for call in peer.calls if call.url.path.endswith("/responses")]) == 1
    assert len(peer.conversations) == 1 and len(env.case.repository.data) == 1
    assert env.case.base.a.runtime.work_policy.records == env.case.base.b.runtime.work_policy.records == {}


@pytest.mark.parametrize("failure", [400, 404, 502, "timeout", 409])
def test_t5_platform_failure_follows_the_shared_error_budget_without_poisoning_the_rid(responses_relay, failure):
    env, command = responses_relay, send_command(responses_relay.case)
    peer = env.remotes[1]
    peer.failure = failure
    first = env.gateway.send(env.case.principal, command)
    assert first.status == "accepted" and first.dispatch.state == "needs_response"
    original = env.case.remotes[1].records[first.message_id]
    assert not any(call.url.path.endswith("/responses") for call in peer.calls)
    if failure == 409:
        pending = env.gateway.send(env.case.principal, command)
        assert pending.status == "accepted" and pending.dispatch.state == "needs_response"
    else:
        with pytest.raises(Problem) as unavailable:
            env.gateway.send(env.case.principal, command)
        assert unavailable.value.status == 503
    assert env.case.remotes[1].records[first.message_id] == original
    assert original.status == "accepted" and original.response_id is None
    peer.failure = None
    followed = env.gateway.send(env.case.principal, command)
    assert followed.dispatch.state == "already_queued" and followed.submit_digest == first.submit_digest
    posted = [call for call in peer.calls if call.url.path.endswith("/responses")]
    assert len(posted) == 2 and all(call.headers["x-client-fa-rid"] == first.message_id for call in posted)
    assert all(call.headers["x-client-fa-submit-digest"] == first.submit_digest for call in posted)
    assert len(peer.conversations) == len(env.case.repository.data) == 1


def test_signed_t5_http_preserves_temporary_unconfirmed_code_and_original_acceptance(request, signing_key):
    from shared_context.behavior_api import _install_error_handlers
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from shared_context.native_agent_api import install_native_agent_routes

    loop = asyncio.new_event_loop()
    try:
        env = request.getfixturevalue("responses_relay")
        app = FastAPI()
        _install_error_handlers(app)
        install_native_agent_routes(app, env.case.base.a, enabled=True, repository=env.case.repository)
        command = send_command(env.case).model_dump(mode="json")
        headers = agent_headers(env.case.base.c, signing_key, 0)
        with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
            accepted = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=command)
            assert accepted.status_code == 202
            original = env.case.remotes[1].records[accepted.json()["message_id"]]
            env.remotes[1].failure = 502
            uncertain = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=command)
            assert uncertain.status_code == 503
            assert uncertain.json()["error"]["code"] == "NATIVE_RESPONSE_UNCONFIRMED"
            assert uncertain.json()["error"]["reconcile_original"] is True
            assert env.case.remotes[1].records[original.request_id] == original
    finally:
        if not loop.is_closed():
            loop.close()


def test_lost_first_peer_ack_reads_the_original_response_lane_without_allocating_a_second_cid(responses_relay):
    env, command = responses_relay, send_command(responses_relay.case)
    env.case.remotes[1].lose_submit = True
    with pytest.raises(Problem) as unknown:
        env.gateway.send(env.case.principal, command)
    assert unknown.value.status == 503
    original, = env.case.remotes[1].records.values()
    assert original.state_profile == NATIVE_RESPONSES_PROFILE and original.status == "accepted"
    env.case.remotes[1].lose_submit = False
    resolved = env.gateway.send(env.case.principal, command)
    assert (resolved.message_id, resolved.conversation_id, resolved.submit_digest) == (
        original.request_id, original.conversation_id, original.submit_digest)
    assert resolved.dispatch.state == "already_queued"
    assert len(env.remotes[1].conversations) == len(env.case.repository.data) == 1


@pytest.mark.parametrize("winner", ["original", "follow"])
@pytest.mark.parametrize("selection_changes", [False, True])
def test_overlapping_first_admissions_reconcile_the_winning_rid_cid_and_capture(
    responses_relay, monkeypatch, winner, selection_changes,
):
    env, command = responses_relay, send_command(responses_relay.case)
    peer, received, reads, replies, errors = env.remotes[1], [], [], {}, {}

    def overlapping(request):
        if "/protocols/openai/" in request.url.path:
            return peer(request)
        body = NATIVE_REQUEST.validate_json(request.content)
        if isinstance(body, NativeRead):
            reads.append((body.request_id, body.request_id in peer.native.records))
        if not isinstance(body, NativeAgentReceive) or body.state_profile != NATIVE_RESPONSES_PROFILE:
            return peer(request)
        received.append((request, body))
        if len(received) == 1:
            if selection_changes:
                control = env.case.base.b.runtime.work_policy
                selected = current_selection(control.store, control.policy)
                select_profile(control, env.case.base.b.assistant.authority, env.case.base.c.human,
                    SelectConversationProfile(request_id="concurrent-response-selection",
                        expected_selection_id=selected.selection_id, template=selected.command.template))
            try:
                replies["follow"] = env.gateway.send(env.case.principal, command)
            except Problem as error:
                errors["follow"] = error.body["error"]["code"]
            return replies["first_control"] if winner == "original" else peer(request)
        if len(received) == 2 and winner == "original":
            replies["first_control"] = peer(received[0][0])
        return peer(request)

    monkeypatch.setattr(env.case.clients[1].http, "_transport", httpx.MockTransport(overlapping))
    try:
        replies["original"] = env.gateway.send(env.case.principal, command)
    except Problem as error:
        errors["original"] = error.body["error"]["code"]
    assert len(reads) >= 2 and not reads[0][1] and not reads[1][1]
    first, second = received[0][1], received[1][1]
    assert first.command.request_id == second.command.request_id
    assert first.new_conversation_id != second.new_conversation_id
    assert (first.capture != second.capture) == selection_changes
    saved, = peer.native.records.values()
    chosen = first if winner == "original" else second
    assert saved.submit_digest == chosen.sha256 and saved.conversation_id == chosen.new_conversation_id
    assert not errors, errors
    for name in ("original", "follow"):
        assert (replies[name].message_id, replies[name].conversation_id, replies[name].submit_digest) == (
            saved.request_id, saved.conversation_id, saved.submit_digest)
    assert any(replies[name].dispatch.state == "already_queued" for name in ("original", "follow"))
    assert len([call for call in peer.calls if call.url.path.endswith("/responses")]) == 1
    assert len(peer.conversations) == 2 and len(env.case.repository.data) == 1


@pytest.mark.parametrize("status,code,reads_expected", [
    (409, "NATIVE_RID_CONFLICT", 2), (409, "NATIVE_CAPTURE_CHANGED", 1), (403, "NATIVE_STATE_SCOPE", 1),
])
def test_fresh_admission_conflict_without_an_original_never_becomes_a_new_send(
    responses_relay, monkeypatch, status, code, reads_expected,
):
    env, command = responses_relay, send_command(responses_relay.case)
    peer, reads, admissions = env.remotes[1], [], []

    def refused(request):
        if "/protocols/openai/" in request.url.path:
            return peer(request)
        body = NATIVE_REQUEST.validate_json(request.content)
        if isinstance(body, NativeRead):
            reads.append(body.request_id)
        if isinstance(body, NativeAgentReceive):
            admissions.append(body)
            return httpx.Response(status, json={"error": {"code": code, "message": "OFFLINE refusal."}})
        return peer(request)

    monkeypatch.setattr(env.case.clients[1].http, "_transport", httpx.MockTransport(refused))
    with pytest.raises(Problem) as failure:
        env.gateway.send(env.case.principal, command)
    assert (failure.value.status, failure.value.body["error"]["code"]) == (status, code)
    assert len(reads) == reads_expected and len(set(reads)) == 1
    assert len(admissions) == len(peer.conversations) == 1 and not peer.native.records
    assert not any(call.url.path.endswith("/responses") for call in peer.calls)


@pytest.mark.parametrize("retained", ["terminal", "changed-material"])
def test_conflict_readback_uses_the_original_terminal_or_refuses_different_material(
    responses_relay, monkeypatch, retained,
):
    env, command = responses_relay, send_command(responses_relay.case)
    peer, admissions, saved = env.remotes[1], [], []

    def conflict(request):
        if "/protocols/openai/" in request.url.path:
            return peer(request)
        body = NATIVE_REQUEST.validate_json(request.content)
        if isinstance(body, NativeAgentReceive):
            admissions.append(body)
            peer(request)
            original = peer.native.records[body.command.request_id]
            different = body.model_copy(update={"command": body.command.model_copy(update={"content": "Other material."})})
            updates = ({"status": "unknown", "failure_code": "NATIVE_TURN_UNKNOWN", "dispatch": None}
                       if retained == "terminal" else {"command": different.command, "submit_digest": different.sha256})
            original = NativeReceipt.model_validate_json(original.model_copy(update=updates).model_dump_json())
            peer.native.records[original.request_id] = original
            saved.append(original)
            return httpx.Response(409, json={"error": {"code": "NATIVE_RID_CONFLICT", "message": "OFFLINE collision."}})
        return peer(request)

    monkeypatch.setattr(env.case.clients[1].http, "_transport", httpx.MockTransport(conflict))
    if retained == "terminal":
        reply = env.gateway.send(env.case.principal, command)
        assert reply.status == "unknown" and reply.submit_digest == saved[0].submit_digest
    else:
        with pytest.raises(Problem) as failure:
            env.gateway.send(env.case.principal, command)
        assert failure.value.body["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert len(admissions) == 1 and peer.native.records[saved[0].request_id] == saved[0]
    assert not any(call.url.path.endswith("/responses") for call in peer.calls)


@pytest.mark.parametrize("origin_profile", [NATIVE_STATE_PROFILE, NATIVE_RESPONSES_PROFILE])
@pytest.mark.parametrize("outcome", [False, True], ids=["result", "unknown-notice"])
def test_peer_result_wake_retains_the_original_a_cid_lane_and_uses_the_same_helper(
    responses_relay, origin_profile, outcome,
):
    env, case = responses_relay, responses_relay.case
    if origin_profile == NATIVE_RESPONSES_PROFILE:
        values = case.sender.capture.model_dump(mode="json")
        values["template"]["settings"]["state_profile"] = origin_profile
        captured = NativeTurnCapture.model_validate_json(json.dumps(values))
        request = NativeSubmit(
            command=case.sender.command, capture=captured, state_profile=origin_profile,
            new_conversation_id=case.sender.conversation_id)
        case.sender = NativeReceipt.model_validate_json(case.sender.model_copy(update={
            "state_profile": origin_profile, "capture": captured, "capture_digest": captured.sha256,
            "submit_digest": request.sha256, "response_id": "resp-original-a",
        }).model_dump_json())
        case.remotes[0].records[case.sender.request_id] = case.sender
    handed = env.gateway.send(case.principal, send_command(case))
    accepted = case.remotes[1].records[handed.message_id]
    text = "Original peer result, with the complete public sample."
    case.remotes[1].text = text
    reference = NativeResultReference(
        turn_number=1, turn_key="turn:1", result_digest="a" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=len(text.encode()),
        text_parts=1, checkpoint_sha256="b" * 64)
    finished = NativeReceipt.model_validate_json(accepted.model_copy(update={
        "status": "unknown" if outcome else "completed", "response_id": "resp-original-b", "dispatch": None,
        "turn_number": 1, "result": None if outcome else reference,
        "failure_code": "NATIVE_TURN_CANCELLED" if outcome else None,
    }).model_dump_json())
    case.remotes[1].records[handed.message_id] = finished
    scope = case.base.a.config.agent_scope(case.base.b.agent_id)
    peer = replace(case.principal, oid=str(scope.identity.oid), actor_id=scope.identity.actor)
    command = NativeRelayResult(conversation_id=finished.conversation_id, request_id=finished.request_id)
    deliver = env.gateway.outcome if outcome else env.gateway.result
    first = deliver(peer, command)
    assert first.message_id == handed.message_id and first.conversation_id == case.sender.conversation_id
    assert first.state_profile == origin_profile
    assert not any(call.url.path.endswith("/responses") for call in env.remotes[0].calls)
    wake = case.remotes[0].records[first.message_id]
    assert wake.source.in_reply_to == handed.message_id
    if outcome:
        assert wake.source.kind == "agent_notice" and wake.source.outcome.status == "unknown"
        assert wake.source.result is None and "NATIVE_TURN_CANCELLED" in wake.command.content
    else:
        assert wake.source.result == reference and wake.command.content == text
    again = deliver(peer, command)
    assert again.submit_digest == first.submit_digest and again.conversation_id == first.conversation_id
    posts = [call for call in env.remotes[0].calls if call.url.path.endswith("/responses")]
    assert len(posts) == (1 if origin_profile == NATIVE_RESPONSES_PROFILE else 0)
    assert not env.remotes[0].conversations and len(case.repository.data) == 1


def test_original_response_peer_material_retains_its_lane_cid_and_digest_on_replay():
    _, scope, recipient, principal, sender = sender_case()
    values = capture().model_dump(mode="json")
    values["template"]["settings"]["state_profile"] = NATIVE_RESPONSES_PROFILE
    selected = NativeTurnCapture.model_validate_json(json.dumps(values))
    command = NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind="request", content="Original brief.")
    material = prepare_request(principal, scope, sender, recipient, selected, command, tool_call_id="original-call")
    request = NATIVE_REQUEST.validate_json(json.dumps({
        **material.model_dump(mode="json"),
        "state_profile": NATIVE_RESPONSES_PROFILE, "new_conversation_id": "original-server-cid",
    }))
    original = NativeReceipt(
        state_profile=NATIVE_RESPONSES_PROFILE, agent_id=recipient.target.agent_id, hosted_session_id="original-peer-sid",
        request_id=request.command.request_id, conversation_id=request.new_conversation_id, command=request.command,
        capture=request.capture, source=request.source, submit_digest=request.sha256, capture_digest=request.capture.sha256,
        status="accepted", dispatch=NativeResponseDispatch(state="needs_response", inflight_request_id=request.command.request_id))
    replay = prepare_request(
        principal, scope, sender, recipient, selected, command, tool_call_id="original-call", original=original)
    assert replay == request and replay.sha256 == original.submit_digest
    assert replay.command.conversation_id is None
    with pytest.raises(Problem) as changed:
        prepare_request(principal, scope, sender, recipient, selected, command.model_copy(update={"content": "Changed brief."}),
                        tool_call_id="original-call", original=original)
    assert changed.value.body["error"]["code"] == "NATIVE_RID_CONFLICT"
