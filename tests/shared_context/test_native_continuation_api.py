"""A signed Agent can dispatch only the original server-retained recovery intent."""

import httpx
import pytest

from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
from test_native_agent_api import (
    instruction_models, jwk_client, peer_runtime, relay_case, relay_client_http, relay_http, signing_key, synthetic_standing_v1,
)
from test_native_turn_api import native_api, PREFIX
from test_native_environment_api import environment_at


@pytest.mark.parametrize("fault", [
    None, (403, "WORK_POLICY_REVOKED"), (403, "NATIVE_CONTINUATION_CAPTURE_REVOKED"),
    (409, "NATIVE_CONTINUATION_ADVANCED"), (409, "NATIVE_CONTINUATION_WORKSPACE_REQUIRED"),
    (409, "NATIVE_SESSION_CHANGED"), (409, "NATIVE_CONTINUATION_CONFLICT"),
    (503, "NATIVE_CONTINUATION_CAPTURE_REVOKED"),
])
def test_signed_continuation_uses_the_retained_intent_and_never_accepts_owner_supplied_lineage(
    relay_client_http, monkeypatch, fault,
):
    from shared_context.behavior_native_turn_wire import (
        NATIVE_REQUEST, NativeContinuationRead, NativeContinuationSubmit, NativeReceipt,
    )
    from shared_context.native_continuation_wire import NativeContinuation, NativeContinuationView
    from shared_context.native_continuations import continuation_request_id

    runner, (case, client, agent, owner) = relay_client_http
    source = NativeReceipt.model_validate_json(case.sender.model_copy(update={
        "status": "unknown", "failure_code": "NATIVE_TURN_UNKNOWN",
    }).model_dump_json())
    case.remotes[0].records[source.request_id] = source
    ref = NativeContinuation(
        continuation_of=source.request_id, root_request_id=source.request_id, attempt=1,
        source_submit_digest=source.submit_digest, checkpoint_sha256="c" * 64)
    rid = continuation_request_id(source.agent_id, source.conversation_id, source.request_id, 1)
    view = NativeContinuationView(
        agent_id=source.agent_id, conversation_id=source.conversation_id, original_request_id=source.request_id,
        root_request_id=source.request_id, request_id=rid, continuation=ref, state="prepared",
        source_hosted_session_id=source.hosted_session_id)
    control_requests = []

    def native(request):
        headers = {
            "content-type": "application/json", "x-agent-session-id": source.hosted_session_id,
            "x-agent-invocation-id": "offline-continuation-invocation",
        }
        command = NATIVE_REQUEST.validate_json(request.content)
        if isinstance(command, NativeContinuationRead):
            assert command.request_id == source.request_id
            return httpx.Response(200, content=view.model_dump_json().encode(), headers=headers)
        if isinstance(command, NativeContinuationSubmit):
            control_requests.append(command)
            retained = case.remotes[0].records.get(rid)
            if retained is None:
                retained = NativeReceipt(
                    agent_id=source.agent_id, hosted_session_id=source.hosted_session_id,
                    request_id=rid, conversation_id=source.conversation_id, command=command.command,
                    capture=command.capture, capture_digest=command.capture.sha256,
                    submit_digest=command.sha256, status="accepted", source=command.source,
                    continuation=command.continuation)
                case.remotes[0].records[rid] = retained
            return httpx.Response(202, content=retained.model_dump_json().encode(), headers=headers)
        return case.remotes[0](request)

    monkeypatch.setattr(case.clients[0].http, "_transport", httpx.MockTransport(native))
    path = NATIVE_AGENT_PREFIX + "/continue"
    query = {"conversation_id": source.conversation_id, "request_id": source.request_id}
    assert client.post(path, headers=owner, json=query).status_code == 403
    assert client.post(path, headers=agent, json={**query, "attempt": 1}).status_code == 422
    if fault is not None:
        from shared_context.errors import Problem
        from shared_context.native_agent_tools import NativeAgentRelayClient

        if fault[1] == "WORK_POLICY_REVOKED":
            case.base.c.control.revoke(case.base.c.human, "revoke-continuation")
        else:
            case.remotes[0].error = fault
        responses = []

        def transport(request):
            result = client.request(request.method, request.url.path, content=request.content, headers=agent)
            responses.append(result)
            return httpx.Response(result.status_code, content=result.content, headers=result.headers)

        async def token():
            return "synthetic-token"

        relay = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(transport))
        with pytest.raises(Problem) as refused:
            runner.run(relay.continue_original(view))
        expected = fault[1] if fault in {
            (403, "WORK_POLICY_REVOKED"), (403, "NATIVE_CONTINUATION_CAPTURE_REVOKED"),
            (409, "NATIVE_CONTINUATION_ADVANCED"),
        } else "NATIVE_RELAY_OUTCOME_UNKNOWN" if fault[0] >= 500 else "NATIVE_RELAY_REFUSED"
        assert refused.value.body["error"]["code"] == expected
        assert len(responses) == 1 and "private upstream details" not in responses[0].text
        assert control_requests == [] and set(case.remotes[0].records) == {source.request_id}
        return
    first = client.post(path, headers=agent, json=query)
    assert first.status_code == 202, first.text
    result = NativeReceipt.model_validate_json(first.content)
    assert result.request_id == rid and result.continuation == ref
    assert result.command.content == source.command.content and result.source == source.source
    second = client.post(path, headers=agent, json=query)
    assert second.status_code == 202 and second.json() == first.json()
    assert len(case.remotes[0].records) == 2
    assert all(value.command.request_id == rid for value in control_requests)
    assert case.remotes[0].records[source.request_id] == source


def test_a_continued_peer_result_returns_under_the_original_message_without_another_root_allowance(
    relay_http, signing_key,
):
    import hashlib
    from shared_context.behavior_native_turn_wire import NativeContinuationSubmit, NativeReceipt, NativeResultReference
    from shared_context.native_continuation_wire import NativeContinuation
    from shared_context.native_continuations import continuation_request_id
    from test_agent_runtime_routing import agent_headers
    from test_native_agent_api import send_command
    from shared_context.native_relay_quota import NativeRelayQuota

    case, client, agent, _owner = relay_http
    sent = client.post(NATIVE_AGENT_PREFIX + "/send", headers=agent,
                       json=send_command(case).model_dump(mode="json"))
    assert sent.status_code == 202
    source = case.remotes[1].records[sent.json()["message_id"]]
    source = NativeReceipt.model_validate_json(source.model_copy(update={
        "status": "unknown", "turn_number": 1, "failure_code": "NATIVE_TURN_UNKNOWN",
    }).model_dump_json())
    case.remotes[1].records[source.request_id] = source
    derived_id = continuation_request_id(source.agent_id, source.conversation_id, source.request_id, 1)
    ref = NativeContinuation(
        continuation_of=source.request_id, root_request_id=source.request_id, attempt=1,
        source_submit_digest=source.submit_digest, checkpoint_sha256="a" * 64)
    request = NativeContinuationSubmit(
        command=source.command.model_copy(update={
            "request_id": derived_id, "conversation_id": source.conversation_id}),
        capture=source.capture, source=source.source, state_profile=source.state_profile, continuation=ref)
    text = "391"
    result = NativeResultReference(
        turn_number=2, turn_key="turn:2", result_digest="b" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=3, text_parts=2,
        checkpoint_sha256="c" * 64)
    derived = NativeReceipt(
        agent_id=source.agent_id, hosted_session_id=source.hosted_session_id,
        request_id=derived_id, conversation_id=source.conversation_id, command=request.command,
        capture=request.capture, submit_digest=request.sha256, capture_digest=request.capture.sha256,
        source=source.source, continuation=ref, status="completed", turn_number=2, result=result)
    case.remotes[1].records[derived_id] = derived
    case.remotes[1].text = text
    peer = agent_headers(case.base.c, signing_key, 1)
    returned = client.post(NATIVE_AGENT_PREFIX + "/result", headers=peer, json={
        "conversation_id": source.conversation_id, "request_id": derived_id})
    assert returned.status_code == 202, returned.text
    assert returned.json()["message_id"] == source.request_id
    wake = case.remotes[0].records[source.request_id]
    assert wake.source.kind == "agent_result" and wake.source.sender.request_id == derived_id
    assert wake.source.in_reply_to == source.request_id and wake.source.result == result
    assert wake.conversation_id == case.sender.conversation_id
    assert case.remotes[1].records[source.request_id] == source
    quota = NativeRelayQuota(case.repository, tenant_id=case.principal.tenant_id, team_id=case.principal.team)
    assert len(quota.read(source.source.root_id).messages) == 1


def test_ready_post_shortcut_reuses_one_notification_and_readonly_paths_never_dispatch(native_api, monkeypatch):
    import json
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, continuation_ready_id
    from shared_context.native_continuation_wire import NativeContinuationReadyReceipt

    case = native_api
    source, calls, _sessions, views = environment_at(case, monkeypatch)
    body = {"request_id": "original-files-follow", "expected_session_id": source.session_id,
            "expected_agent_version": "42"}
    old_transport = case.foundry.http._transport
    notified = []

    def transport(request):
        raw = json.loads(request.content)
        if raw["operation"] != "continuation_ready":
            return old_transport.handle_request(request)
        command = NATIVE_REQUEST.validate_json(request.content)
        notified.append(command.model_dump(mode="json"))
        assert command.request_id == continuation_ready_id(
            body["request_id"], views[0].hosted_session_id, views[0].files_manifest_sha256)
        if len(notified) == 1:
            raise httpx.ReadTimeout("OFFLINE ready notification ACK was lost", request=request)
        reply = NativeContinuationReadyReceipt(
            agent_id=source.agent_id, hosted_session_id=views[0].hosted_session_id,
            request_id=command.request_id, environment_request_id=body["request_id"],
            pending_request_ids=("original-interrupted-rid",))
        return httpx.Response(200, content=reply.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-session-id": views[0].hosted_session_id,
            "x-agent-invocation-id": "original-ready-notification",
        })

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(transport))
    pending = case.client.post(PREFIX + "/environment", headers=case.headers, json=body)
    assert pending.status_code == 200 and pending.json()["model_started"] is False
    assert pending.json()["state"] == "ready"
    assert len(notified) == 1
    assert case.client.get(PREFIX + "/environment", headers=case.headers).status_code == 200
    assert case.client.post(PREFIX + "/environment/reconcile", headers=case.headers, json=body).status_code == 200
    assert len(notified) == 1
    again = case.client.post(PREFIX + "/environment", headers=case.headers, json=body)
    assert again.status_code == 200 and again.json() == pending.json()
    assert len(notified) == 2 and notified[0] == notified[1]
    assert sum(kind == "create" for kind, _sid in calls) == 1
    assert sum(kind == "stop" for kind, _sid in calls) == 1
