"""Group owner/Agent control routes through the real signed Team entry."""

import pytest

from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
from shared_context.errors import Problem
from test_native_agent_api import (
    instruction_models, jwk_client, peer_runtime, relay_case, relay_client_http, relay_http, signing_key,
    synthetic_standing_v1,
)
from test_native_responses_relay_api import responses_relay


def test_group_start_uses_one_original_responses_root_and_never_launches_other_members(responses_relay, monkeypatch):
    from shared_context.native_group_api import NativeGroupGateway
    from shared_context.native_group_wire import NativeGroupOpen

    env = responses_relay
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    gateway = NativeGroupGateway(env.case.base.a, enabled=True, repository=env.case.repository)
    owner = env.case.base.c.human
    command = NativeGroupOpen(
        request_id="human-group-start", orchestrator_agent_id=env.case.base.b.agent_id,
        member_agent_ids=(env.case.sender.agent_id, env.case.base.b.agent_id), content="Discuss these public choices.",
    )
    group = gateway.open(owner, command)
    started = gateway.start(owner, group.group_id)
    assert started.group_id == group.group_id
    assert started.receipt.agent_id == group.orchestrator_agent_id
    assert started.receipt.request_id == group.group_id and started.receipt.command.content == command.content
    assert started.receipt.state_profile == "foundry-responses-v1"
    assert gateway.start(owner, group.group_id).receipt == started.receipt
    assert len(env.case.remotes[1].records) == 1 and len(env.case.remotes[0].records) == 1
    # The retried start is not pre-read: Hosted's create-or-read keeps the original receipt and CID, leaving at most
    # one unreferenced empty platform conversation (as a refused T5 admission already may).
    assert env.remotes[1].conversations == ["conv-peer-1", "conv-peer-2"] and not env.remotes[0].conversations
    assert started.receipt.conversation_id == "conv-peer-1"


def test_group_context_uses_the_original_native_root_not_a_model_supplied_group(responses_relay, monkeypatch):
    from dataclasses import replace
    from shared_context.behavior_native_agent_wire import NativeRelayResult
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.native_group_api import NativeGroupGateway
    from shared_context.native_group_wire import NativeGroupOpen

    env = responses_relay
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    gateway = NativeGroupGateway(env.case.base.a, enabled=True, repository=env.case.repository)
    group = gateway.open(env.case.base.c.human, NativeGroupOpen(
        request_id="bound-context", orchestrator_agent_id=env.case.base.b.agent_id,
        member_agent_ids=(env.case.sender.agent_id, env.case.base.b.agent_id), content="Keep the original group root.",
    ))
    accepted = gateway.start(env.case.base.c.human, group.group_id).receipt
    current = NativeReceipt.model_validate_json(accepted.model_copy(update={
        "status": "started", "turn_number": 1, "response_id": "resp-group-root", "dispatch": None,
    }).model_dump_json())
    env.case.remotes[1].records[current.request_id] = current
    scope = env.case.base.b.config.agent_scope(env.case.base.b.agent_id)
    principal = replace(env.case.principal, oid=str(scope.identity.oid), actor_id=scope.identity.actor)
    context = gateway.context(principal, NativeRelayResult(
        conversation_id=current.conversation_id, request_id=current.request_id))
    assert context.group.group_id == group.group_id and context.role == "orchestrator"
    assert context.root.request_id == current.request_id
    assert gateway.context(env.case.principal, NativeRelayResult(
        conversation_id=env.case.sender.conversation_id, request_id=env.case.sender.request_id)).group is None


def test_original_t5_group_request_publishes_once_and_keeps_its_original_root_quota(responses_relay, monkeypatch):
    from dataclasses import replace
    import json
    from shared_context.behavior_agent_template import AgentTemplate
    from shared_context.behavior_native_agent_wire import NativeRelaySend
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.behavior_profile_selection import SelectConversationProfile, current_selection, select_profile
    from shared_context.native_group_api import NativeGroupGateway
    from shared_context.native_group_wire import NativeGroupOpen

    env = responses_relay
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    monkeypatch.setenv("FA_NATIVE_GROUPS_ENABLED", "1")
    control = env.case.base.a.runtime.work_policy
    selection = current_selection(control.store, control.policy)
    template = env.case.sender.capture.template.model_dump(mode="json")
    template["settings"]["state_profile"] = "foundry-responses-v1"
    select_profile(control, env.case.base.a.assistant.authority, env.case.base.c.human, SelectConversationProfile(
        request_id="group-member-profile", expected_selection_id=None if selection is None else selection.selection_id,
        template=AgentTemplate.model_validate_json(json.dumps(template))))
    groups = NativeGroupGateway(env.case.base.a, enabled=True, repository=env.case.repository)
    group = groups.open(env.case.base.c.human, NativeGroupOpen(
        request_id="group-relay-root", orchestrator_agent_id=env.case.base.b.agent_id,
        member_agent_ids=(env.case.sender.agent_id, env.case.base.b.agent_id), content="Compare the original choices.",
    ))
    accepted = groups.start(env.case.base.c.human, group.group_id).receipt
    sender = NativeReceipt.model_validate_json(accepted.model_copy(update={
        "status": "started", "turn_number": 1, "response_id": "resp-group-root", "dispatch": None,
    }).model_dump_json())
    env.case.remotes[1].records[sender.request_id] = sender
    scope = env.case.base.b.config.agent_scope(env.case.base.b.agent_id)
    principal = replace(env.case.principal, oid=str(scope.identity.oid), actor_id=scope.identity.actor)
    request = NativeRelaySend(conversation_id=sender.conversation_id, request_id=sender.request_id,
        tool_call_id="original-group-call", message={
            "recipient_agent_id": env.case.sender.agent_id, "kind": "request", "content": "Check option one."})
    received = env.gateway.send(principal, request)
    env.gateway.send(principal, request)
    view = groups.read(env.case.base.c.human, group.group_id)
    assert len(view.posts) == 1 and view.posts[0].speaker_agent_id == sender.agent_id
    assert view.posts[0].kind == "request" and view.awaiting_final
    child = env.case.remotes[0].records[received.message_id]
    assert child.source.root.request_id == group.group_id and child.state_profile == "foundry-responses-v1"
    assert len(env.gateway._quota(principal).read(child.source.root_id).messages) == 1
    assert len(view.deliveries) == 1 and view.deliveries[0].message_id == received.message_id
    assert view.deliveries[0].status == "awaiting" and view.deliveries[0].round == 1


@pytest.mark.parametrize("outcome", ["completed", "unknown", "stopped-continuation"])
@pytest.mark.parametrize("finish_before_publication", [False, True])
def test_pending_group_reply_keeps_its_round_and_slot_until_the_original_result_is_saved(
    responses_relay, monkeypatch, outcome, finish_before_publication,
):
    from dataclasses import replace
    from shared_context.behavior_native_agent_wire import NativeRelayResult
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.native_group_api import NativeGroupGateway
    from shared_context.native_group_wire import NativeGroupAdvance, NativeGroupOpen
    from test_native_agent_api import send_command

    env = responses_relay
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    monkeypatch.setenv("FA_NATIVE_GROUPS_ENABLED", "1")
    groups = NativeGroupGateway(env.case.base.a, enabled=True, repository=env.case.repository)
    # Use the already started A turn as the original human root, with a real group ID.
    group = groups.open(env.case.base.c.human, NativeGroupOpen(
        request_id="await-group-result", orchestrator_agent_id=env.case.sender.agent_id,
        member_agent_ids=(env.case.sender.agent_id, env.case.base.b.agent_id), content=env.case.sender.command.content))
    sender = env.case.sender
    original_request = sender.command.model_copy(update={"request_id": group.group_id})
    from shared_context.behavior_native_turn_wire import NativeSubmit
    capture = sender.capture.model_copy(update={"template": sender.capture.template.model_copy(update={
        "settings": sender.capture.template.settings.model_copy(update={"state_profile": "foundry-responses-v1"})})})
    submitted = NativeSubmit(command=original_request, capture=capture, state_profile="foundry-responses-v1",
                             new_conversation_id="group-original-cid")
    sender = NativeReceipt.model_validate_json(sender.model_copy(update={
        "request_id": group.group_id, "command": original_request, "capture": capture,
        "conversation_id": "group-original-cid",
        "capture_digest": capture.sha256, "submit_digest": submitted.sha256,
        "state_profile": "foundry-responses-v1", "response_id": "resp-group-a",
    }).model_dump_json())
    env.case.sender = sender
    env.case.remotes[0].records[group.group_id] = sender
    handed = env.gateway.send(env.case.principal, send_command(env.case))
    with pytest.raises(Problem) as pending:
        groups.advance(env.case.principal, group.group_id,
                       NativeGroupAdvance(request_id="premature-next-round", expected_round=1))
    assert pending.value.body["error"]["code"] == "NATIVE_GROUP_PENDING"
    child = env.case.remotes[1].records[handed.message_id]
    env.case.remotes[1].records[handed.message_id] = NativeReceipt.model_validate_json(child.model_copy(update={
        "status": "started", "turn_number": 1, "response_id": "resp-group-b", "dispatch": None,
    }).model_dump_json())
    env.case.remotes[1].completed(handed.message_id)
    completed = env.case.remotes[1].records[handed.message_id]
    if outcome != "completed":
        completed = NativeReceipt.model_validate_json(completed.model_copy(update={
            "status": "unknown", "result": None, "failure_code": "NATIVE_TURN_CANCELLED",
            "continuation_available": outcome == "stopped-continuation",
        }).model_dump_json())
        env.case.remotes[1].records[handed.message_id] = completed
    scope = env.case.base.b.config.agent_scope(env.case.base.b.agent_id)
    actor = replace(env.case.principal, oid=str(scope.identity.oid), actor_id=scope.identity.actor)
    command = NativeRelayResult(conversation_id=completed.conversation_id, request_id=completed.request_id)
    if outcome == "stopped-continuation":
        import httpx
        import json
        from shared_context.behavior_native_turn_wire import continuation_request_id
        from shared_context.native_continuation_wire import NativeContinuation, NativeContinuationView

        reference = NativeContinuation(
            continuation_of=completed.request_id, root_request_id=completed.request_id, attempt=1,
            source_submit_digest=completed.submit_digest, checkpoint_sha256="c" * 64)
        continuation = NativeContinuationView(
            agent_id=completed.agent_id, conversation_id=completed.conversation_id,
            original_request_id=completed.request_id, root_request_id=completed.request_id,
            request_id=continuation_request_id(
                completed.agent_id, completed.conversation_id, completed.request_id, 1),
            continuation=reference, state="prepared", source_hosted_session_id=completed.hosted_session_id)
        transport = env.case.clients[1].http._transport

        def read_continuation(packet):
            value = json.loads(packet.content)
            if value.get("operation") == "continuation":
                assert value["request_id"] == completed.request_id
                return httpx.Response(200, content=continuation.model_dump_json().encode(), headers={
                    "content-type": "application/json", "x-agent-session-id": completed.hosted_session_id,
                    "x-agent-invocation-id": "offline-continuation-read",
                })
            return transport.handle_request(packet)

        monkeypatch.setattr(env.case.clients[1].http, "_transport", httpx.MockTransport(read_continuation))
        assert groups.complete(actor, command).reason == "awaiting_results"
        with pytest.raises(Problem) as waiting:
            env.gateway.outcome(actor, command)
        assert waiting.value.body["error"]["code"] == "NATIVE_CONTINUATION_PENDING"
        assert handed.message_id not in env.case.remotes[0].records
        continuation = continuation.model_copy(update={
            "state": "blocked", "failure_code": "NATIVE_TURN_CANCELLED", "cancel_requested": True})
    if finish_before_publication:
        import hashlib
        from shared_context.behavior_native_turn_wire import NativeResultReference

        replace_record, lost = env.case.repository.replace, []

        def first_publication_lost(*args):
            if args[1] == "group:" + group.group_id and not lost:
                lost.append(True)
                raise Problem(503, "OFFLINE_PUBLICATION_UNAVAILABLE", "OFFLINE result publication did not commit")
            return replace_record(*args)

        monkeypatch.setattr(env.case.repository, "replace", first_publication_lost)
        delivered = (env.gateway.result if outcome == "completed" else env.gateway.outcome)(actor, command)
        assert delivered.status == "accepted" and lost
        wake = env.case.remotes[0].records[delivered.message_id]
        text = "The saved conclusion includes the member outcome and its remaining uncertainty."
        env.case.remotes[0].text = text
        reference = NativeResultReference(turn_number=2, turn_key="turn:2", result_digest="a" * 64,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=len(text.encode()), text_parts=1,
            checkpoint_sha256="b" * 64)
        wake = NativeReceipt.model_validate_json(wake.model_copy(update={
            "status": "completed", "turn_number": 2, "response_id": "resp-group-final",
            "dispatch": None, "result": reference,
        }).model_dump_json())
        env.case.remotes[0].records[wake.request_id] = wake
        final_command = NativeRelayResult(conversation_id=wake.conversation_id, request_id=wake.request_id)
        deferred = groups.complete(env.case.principal, final_command)
        assert deferred.reason == "awaiting_results"
        assert groups.read(env.case.base.c.human, group.group_id).awaiting_final
        retried = groups.complete(actor, command)
        assert retried.reason == "published"
        closed = groups.read(env.case.base.c.human, group.group_id)
        assert closed.state == "closed" and not closed.awaiting_final and len(closed.posts) == 3
        assert closed.posts[-1].kind == "final" and closed.posts[-1].result == reference
        assert groups.complete(env.case.principal, final_command).post == closed.posts[-1]
        assert groups.complete(actor, command) == retried
        assert len(groups.read(env.case.base.c.human, group.group_id).posts) == 3
        if outcome == "stopped-continuation":
            repeated = env.gateway.outcome(actor, command)
            assert repeated.message_id == delivered.message_id
            assert len(groups.read(env.case.base.c.human, group.group_id).posts) == 3
        return
    posted = groups.complete(actor, command)
    assert posted.reason == "published" and posted.post.round == 1
    assert posted.post.kind == ("result" if outcome == "completed" else "blocked")
    groups.advance(env.case.principal, group.group_id,
                   NativeGroupAdvance(request_id="next-after-result", expected_round=1))
    assert groups.complete(actor, command) == posted
    state = groups.read(env.case.base.c.human, group.group_id)
    assert len(state.posts) == 2 and state.round == 2 and state.awaiting_final
    assert state.deliveries[0].status == ("completed" if outcome == "completed" else "blocked")
    if outcome == "stopped-continuation":
        before = len(env.case.remotes[0].records)
        first = env.gateway.outcome(actor, command)
        repeated = env.gateway.outcome(actor, command)
        assert first.message_id == repeated.message_id == handed.message_id
        assert len(env.case.remotes[0].records) == before + 1
        assert len(groups.read(env.case.base.c.human, group.group_id).posts) == 2
        assert len(env.gateway._quota(actor).read(child.source.root_id).messages) == 1


@pytest.mark.parametrize(("refusal", "fault"), [
    ("UPSTREAM_UNKNOWN", None), ("NATIVE_TURNS_DISABLED", None), ("NATIVE_SESSION_CHANGED", None),
    ("NATIVE_TURNS_DISABLED", "conflict"), ("NATIVE_TURNS_DISABLED", "lost-ack"),
])
def test_a_definite_unaccepted_member_refusal_settles_only_its_original_group_slot(
    responses_relay, monkeypatch, refusal, fault,
):
    import json
    import httpx
    from shared_context.behavior_native_turn_wire import (
        NATIVE_REQUEST, NativeAgentReceive, NativeReceipt, NativeSubmit, NativeTurnCapture,
    )
    from shared_context.native_group_api import NativeGroupGateway
    from shared_context.native_group_wire import NativeGroupAdvance, NativeGroupOpen
    from test_native_agent_api import send_command

    env = responses_relay
    definite = refusal != "UPSTREAM_UNKNOWN"
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    monkeypatch.setenv("FA_NATIVE_GROUPS_ENABLED", "1")
    groups = NativeGroupGateway(env.case.base.a, enabled=True, repository=env.case.repository)
    group = groups.open(env.case.base.c.human, NativeGroupOpen(
        request_id="refused-before-admission", orchestrator_agent_id=env.case.sender.agent_id,
        member_agent_ids=(env.case.sender.agent_id, env.case.base.b.agent_id), content=env.case.sender.command.content))
    source = env.case.sender
    values = source.capture.model_dump(mode="json")
    values["template"]["settings"]["state_profile"] = "foundry-responses-v1"
    capture = NativeTurnCapture.model_validate_json(json.dumps(values))
    command = source.command.model_copy(update={"request_id": group.group_id})
    request = NativeSubmit(command=command, capture=capture, state_profile="foundry-responses-v1",
                           new_conversation_id="refused-root-cid")
    source = NativeReceipt.model_validate_json(source.model_copy(update={
        "request_id": group.group_id, "conversation_id": "refused-root-cid", "command": command,
        "capture": capture, "capture_digest": capture.sha256, "submit_digest": request.sha256,
        "state_profile": "foundry-responses-v1", "response_id": "resp-refused-root",
    }).model_dump_json())
    env.case.sender = source
    env.case.remotes[0].records[source.request_id] = source

    def cannot_dispatch(request):
        if refusing and "/protocols/openai/" not in request.url.path and isinstance(
                NATIVE_REQUEST.validate_json(request.content), NativeAgentReceive):
            # The real Hosted contract: an unenabled lane is 403, a changed session 409, unknown 503.
            status = 503 if not definite else 403 if refusal == "NATIVE_TURNS_DISABLED" else 409
            return httpx.Response(status, json={"error": {
                "code": refusal, "message": "OFFLINE selected Responses version is unavailable"}})
        return env.remotes[1](request)

    refusing = [True]
    monkeypatch.setattr(env.case.clients[1].http, "_transport", httpx.MockTransport(cannot_dispatch))
    replace_record, failed_write = env.case.repository.replace, []

    def refusal_write(*args):
        if (fault and not failed_write and args[1] == "group:" + group.group_id
                and any(item.get("refusal_code") for item in args[2]["view"]["deliveries"])):
            failed_write.append(True)
            if fault == "lost-ack":
                replace_record(*args)
            raise Problem(412 if fault == "conflict" else 503, "OFFLINE_WRITE_UNKNOWN", "OFFLINE refusal write")
        return replace_record(*args)

    monkeypatch.setattr(env.case.repository, "replace", refusal_write)
    original = send_command(env.case)
    with pytest.raises(Problem) as first_error:
        env.gateway.send(env.case.principal, original)
    if fault == "conflict":
        assert first_error.value.status == 503 and first_error.value.body["error"]["code"] == "NATIVE_GROUP_UNKNOWN"
        assert groups.read(env.case.base.c.human, group.group_id).deliveries[0].status == "awaiting"
        with pytest.raises(Problem) as retried:
            env.gateway.send(env.case.principal, original)
        assert retried.value.status == 403 and retried.value.body["error"]["code"] == refusal
    view = groups.read(env.case.base.c.human, group.group_id)
    assert not env.case.remotes[1].records
    if definite:
        assert view.deliveries[0].status == "blocked"
        assert [post.kind for post in view.posts] == ["request"]
        assert view.deliveries[0].refusal_code == refusal
        assert groups.advance(env.case.principal, group.group_id,
            NativeGroupAdvance(request_id="after-refusal", expected_round=1)).to_round == 2
        refusing.clear()
        with pytest.raises(Problem) as retained:
            env.gateway.send(env.case.principal, original)
        assert retained.value.body["error"]["code"] == "NATIVE_GROUP_REQUEST_REJECTED"
        assert not env.case.remotes[1].records
    else:
        assert view.deliveries[0].status == "awaiting" and len(view.posts) == 1
        with pytest.raises(Problem) as pending:
            groups.advance(env.case.principal, group.group_id,
                           NativeGroupAdvance(request_id="not-proven-refused", expected_round=1))
        assert pending.value.body["error"]["code"] == "NATIVE_GROUP_PENDING"


def test_group_final_is_published_from_the_saved_native_result_not_model_supplied_text(responses_relay, monkeypatch):
    import hashlib
    from dataclasses import replace
    from shared_context.behavior_native_agent_wire import NativeRelayResult
    from shared_context.behavior_native_turn_wire import NativeReceipt, NativeResultReference
    from shared_context.native_group_api import NativeGroupGateway
    from shared_context.native_group_wire import NativeGroupOpen

    env = responses_relay
    monkeypatch.setenv("FA_NATIVE_TURNS", "foundry-native-v1")
    groups = NativeGroupGateway(env.case.base.a, enabled=True, repository=env.case.repository)
    group = groups.open(env.case.base.c.human, NativeGroupOpen(
        request_id="original-final", orchestrator_agent_id=env.case.base.b.agent_id,
        member_agent_ids=(env.case.sender.agent_id, env.case.base.b.agent_id), content="Give the original final.",
    ))
    accepted = groups.start(env.case.base.c.human, group.group_id).receipt
    text = "This is the saved orchestrator conclusion."
    env.case.remotes[1].text = text
    reference = NativeResultReference(turn_number=1, turn_key="turn:1", result_digest="a" * 64,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), text_bytes=len(text.encode()), text_parts=1,
        checkpoint_sha256="b" * 64)
    done = NativeReceipt.model_validate_json(accepted.model_copy(update={
        "status": "completed", "turn_number": 1, "response_id": "resp-final", "dispatch": None, "result": reference,
    }).model_dump_json())
    env.case.remotes[1].records[done.request_id] = done
    scope = env.case.base.b.config.agent_scope(env.case.base.b.agent_id)
    actor = replace(env.case.principal, oid=str(scope.identity.oid), actor_id=scope.identity.actor)
    command = NativeRelayResult(conversation_id=done.conversation_id, request_id=done.request_id)
    complete = groups.complete(actor, command)
    assert complete.post.kind == "final" and complete.reason == "published"
    assert groups.complete(actor, command) == complete
    view = groups.read(env.case.base.c.human, group.group_id)
    assert view.state == "closed" and not view.awaiting_final and len(view.posts) == 1
    original = groups.read_post(env.case.base.c.human, group.group_id, complete.post.post_id)
    assert original.content == text and original.reference.result == reference
    assert original.reference.turn.request_id == done.request_id


def test_group_control_is_default_off_and_never_creates_state(relay_http):
    from shared_context.native_group_api import install_native_group_routes

    case, client, _agent, owner = relay_http
    install_native_group_routes(client.app, case.base.a, repository=case.repository, environ={})
    options = client.get("/native-groups/v1/options", headers=owner)
    assert options.status_code == 200
    assert options.json()["enabled"] is False
    denied = client.post("/native-groups/v1/groups", headers=owner, json={
        "request_id": "disabled-group", "orchestrator_agent_id": case.sender.agent_id,
        "member_agent_ids": [case.sender.agent_id, case.base.b.agent_id], "content": "Do not enable implicitly.",
    })
    assert denied.status_code == 503 and denied.json()["error"]["code"] == "NATIVE_GROUP_DISABLED"
    assert case.repository.data == {}


def test_only_the_current_owner_opens_a_scoped_group_and_reads_the_original_state(relay_http):
    from shared_context.native_group_api import install_native_group_routes

    case, client, agent, owner = relay_http
    install_native_group_routes(client.app, case.base.a, repository=case.repository,
                                environ={"FA_NATIVE_GROUPS_ENABLED": "1"})
    command = {
        "request_id": "owner-opened-group", "orchestrator_agent_id": case.sender.agent_id,
        "member_agent_ids": [case.sender.agent_id, case.base.b.agent_id], "content": "Discuss the public choices.",
    }
    assert client.post("/native-groups/v1/groups", headers=agent, json=command).status_code == 403
    assert not case.repository.data
    first = client.post("/native-groups/v1/groups", headers=owner, json=command)
    assert first.status_code == 201, first.text
    group_id = first.json()["group_id"]
    again = client.post("/native-groups/v1/groups", headers=owner, json=command)
    assert again.json() == first.json()
    read = client.get("/native-groups/v1/groups/" + group_id, headers=owner)
    assert read.status_code == 200 and read.json() == first.json()
    forged = client.post("/native-groups/v1/groups", headers=owner, json={**command, "team_id": "another-team"})
    assert forged.status_code == 422
    assert len(case.repository.data) == 1
    assert client.get(NATIVE_AGENT_PREFIX + "/recipients", headers=agent).status_code == 200


def test_agent_group_posts_keep_verified_speaker_and_typed_silence_is_not_human_append(relay_http):
    from shared_context.native_group_api import install_native_group_routes

    case, client, agent, owner = relay_http
    install_native_group_routes(client.app, case.base.a, repository=case.repository,
                                environ={"FA_NATIVE_GROUPS_ENABLED": "1"})
    opened = client.post("/native-groups/v1/groups", headers=owner, json={
        "request_id": "agent-post-group", "orchestrator_agent_id": case.sender.agent_id,
        "member_agent_ids": [case.sender.agent_id, case.base.b.agent_id], "content": "Keep source identities distinct.",
    })
    assert opened.status_code == 201, opened.text
    path = "/native-groups/v1/groups/" + opened.json()["group_id"]
    body = {"request_id": "original-agent-post", "round": 1, "kind": "fyi", "content": "One public observation."}
    assert client.post(path + "/posts", headers=owner, json=body).status_code == 403
    sent = client.post(path + "/posts", headers=agent, json=body)
    assert sent.status_code == 201, sent.text
    assert sent.json()["speaker_agent_id"] == case.sender.agent_id
    original = client.get(path + "/posts/" + sent.json()["post_id"], headers=owner)
    assert original.status_code == 200 and original.json()["content"] == body["content"]
    assert client.post(path + "/posts", headers=agent, json={**body, "speaker_agent_id": case.base.b.agent_id}).status_code == 422
    signal = client.post(path + "/signals", headers=agent, json={
        "event_id": "d" * 64, "recipient_agent_id": case.base.b.agent_id,
        "kind": "fyi", "payload_sha256": sent.json()["content_sha256"],
    })
    assert signal.status_code == 200 and signal.json()["action"] == "silence" and not signal.json()["start_work"]
    read = client.get(path, headers=owner).json()
    assert read["awaiting_final"] and len(read["posts"]) == 1
    assert len(case.remotes[0].records) == 1 and not case.remotes[1].records


@pytest.fixture
def group_tool_http(relay_client_http, responses_relay, monkeypatch):
    import json
    from types import SimpleNamespace
    import httpx
    from shared_context.behavior_native_turn_wire import NativeReceipt, NativeSubmit, NativeTurnCapture
    from shared_context.native_group_api import install_native_group_routes
    from shared_context.native_group_tools import NativeGroupClient, NativeGroupTools

    runner, (case, client, agent, owner) = relay_client_http
    monkeypatch.setenv("FA_NATIVE_GROUPS_ENABLED", "1")
    install_native_group_routes(client.app, case.base.a, repository=case.repository,
                                environ={"FA_NATIVE_GROUPS_ENABLED": "1"})
    opened = client.post("/native-groups/v1/groups", headers=owner, json={
        "request_id": "group-tool-feedback", "orchestrator_agent_id": case.sender.agent_id,
        "member_agent_ids": [case.sender.agent_id, case.base.b.agent_id],
        "content": case.sender.command.content,
    })
    assert opened.status_code == 201, opened.text
    group_id = opened.json()["group_id"]
    values = case.sender.capture.model_dump(mode="json")
    values["template"]["settings"]["state_profile"] = "foundry-responses-v1"
    capture = NativeTurnCapture.model_validate_json(json.dumps(values))
    command = case.sender.command.model_copy(update={"request_id": group_id})
    submitted = NativeSubmit(command=command, capture=capture, state_profile="foundry-responses-v1",
                             new_conversation_id=case.sender.conversation_id)
    receipt = NativeReceipt.model_validate_json(case.sender.model_copy(update={
        "request_id": group_id, "command": command, "capture": capture,
        "capture_digest": capture.sha256, "submit_digest": submitted.sha256,
        "state_profile": "foundry-responses-v1", "response_id": "resp-group-tool-feedback",
    }).model_dump_json())
    case.sender = receipt
    case.remotes[0].records[group_id] = receipt
    tools = NativeGroupTools(NativeGroupClient(
        "https://testserver", lambda: agent["Authorization"].removeprefix("Bearer "),
        transport=httpx.ASGITransport(app=client.app)),
        receipt, SimpleNamespace(stop_requested=lambda: False))
    offered = {tool.name: tool for tool in runner.run(tools.prepare())}
    return SimpleNamespace(runner=runner, case=case, client=client, agent=agent, owner=owner,
                           group_id=group_id, tools=offered)


def test_next_group_round_schema_describes_current_round_and_concurrency(group_tool_http):
    tool = group_tool_http.tools["next_group_round"]
    description = tool.parameters["properties"]["expected_round"].get("description", "")
    assert all(word in description for word in ("目前輪次", "不是要切換", "並發", "read_group"))
    assert "已到原預期輪次就不要再換輪" in description


def test_next_group_round_mismatch_guides_read_then_advance_without_permission_claim(group_tool_http):
    import json
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.m2_copilot import ToolFailure
    from shared_context.native_group_wire import NativeGroupRoundReceipt

    env = group_tool_http
    tool = env.tools["next_group_round"]
    path = "/native-groups/v1/groups/" + env.group_id
    original = env.client.get(path, headers=env.owner).json()
    with pytest.raises(ToolFailure) as wrong_round:
        env.runner.run(tool.handler({"tool_call_id": "wrong-target-round", "arguments": {"expected_round": 2}}))
    assert env.client.get(path, headers=env.owner).json() == original
    feedback = wrong_round.value.public_message
    assert "NATIVE_GROUP_ROUND_CHANGED" in feedback
    assert all(word in feedback for word in ("輪次不符", "read_group", "目前輪次", "不是權限拒絕"))
    assert "已到原預期輪次就不要再換輪" in feedback
    assert wrong_round.value.public_code == "NATIVE_GROUP_ROUND_CHANGED"
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results={
        "next_group_round": NativeGroupRoundReceipt})
    assert boundary.public_tool_failure("next_group_round", wrong_round.value.public_code) == feedback

    read = env.runner.run(env.tools["read_group"].handler({"tool_call_id": "read-current", "arguments": {}}))
    current_round = json.loads(read.text)["group"]["round"]
    assert current_round == 1
    result = env.runner.run(tool.handler({
        "tool_call_id": "advance-read-round", "arguments": {"expected_round": current_round}}))
    assert json.loads(result.text)["to_round"] == 2
    replay = env.runner.run(tool.handler({
        "tool_call_id": "advance-read-round", "arguments": {"expected_round": current_round}}))
    assert replay == result and env.client.get(path, headers=env.owner).json()["round"] == 2
    with pytest.raises(ToolFailure) as stale:
        env.runner.run(tool.handler({"tool_call_id": "stale-round", "arguments": {"expected_round": 1}}))
    assert stale.value.public_message == feedback
    assert env.client.get(path, headers=env.owner).json()["round"] == 2


@pytest.mark.parametrize("status", [403, 503])
def test_round_conflict_feedback_does_not_reclassify_other_statuses(group_tool_http, monkeypatch, status):
    from shared_context.m2_copilot import ToolFailure
    from shared_context.native_group_api import NativeGroupGateway

    def refused(*_args):
        raise Problem(status, "NATIVE_GROUP_ROUND_CHANGED", "PRIVATE_UPSTREAM_DETAILS")

    monkeypatch.setattr(NativeGroupGateway, "turn_advance", refused)
    env = group_tool_http
    with pytest.raises(ToolFailure) as failure:
        env.runner.run(env.tools["next_group_round"].handler({
            "tool_call_id": "not-round-conflict", "arguments": {"expected_round": 1}}))
    assert failure.value.public_code is None
    assert "不是權限拒絕" not in failure.value.public_message
    assert "PRIVATE_UPSTREAM_DETAILS" not in failure.value.public_message
    assert env.client.get("/native-groups/v1/groups/" + env.group_id, headers=env.owner).json()["round"] == 1


@pytest.mark.parametrize("kind", ["request", "handoff"])
def test_member_circular_request_explains_original_result_return_through_signed_api(
    group_tool_http, signing_key, kind,
):
    import httpx
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.behavior_native_agent_wire import NativePeerRequest, NativeRelayAcceptance, NativeRelayResult
    from shared_context.behavior_native_turn_wire import NativeReceipt
    from shared_context.native_agent_tools import NativeAgentRelayClient
    from test_agent_runtime_routing import agent_headers
    from test_native_agent_api import send_command

    env = group_tool_http
    sent = env.client.post(NATIVE_AGENT_PREFIX + "/send", headers=env.agent,
                           json=send_command(env.case).model_dump(mode="json"))
    assert sent.status_code == 202, sent.text
    message_id = sent.json()["message_id"]
    member = env.case.remotes[1].records[message_id]
    member = NativeReceipt.model_validate_json(member.model_copy(update={
        "status": "started", "turn_number": 1, "response_id": "resp-original-member", "dispatch": None,
    }).model_dump_json())
    env.case.remotes[1].records[message_id] = member
    member_headers = agent_headers(env.case.base.c, signing_key, 1)

    async def token():
        return member_headers["Authorization"].removeprefix("Bearer ")

    hosted = NativeAgentRelayClient(
        "https://testserver", token, transport=httpx.ASGITransport(app=env.client.app))
    path = "/native-groups/v1/groups/" + env.group_id
    original_group = env.client.get(path, headers=env.owner).json()
    original_receipts = [dict(remote.records) for remote in env.case.remotes]

    with pytest.raises(Problem) as circular:
        env.runner.run(hosted.send(member, "circular-request", NativePeerRequest(
            recipient_agent_id=env.case.sender.agent_id, kind=kind, content="This is a result, not new work.")))
    assert circular.value.status == 409
    assert circular.value.body["error"]["code"] == "NATIVE_GROUP_REPLY_REQUIRED"
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results={
        "send_agent_message": NativeRelayAcceptance})
    feedback = boundary.public_tool_failure("send_agent_message", circular.value.body["error"]["code"])
    assert all(word in feedback for word in ("主持者", "循環", "原結果", "send_agent_message"))
    assert "Authority refused" not in feedback
    assert env.client.get(path, headers=env.owner).json() == original_group
    assert [remote.records for remote in env.case.remotes] == original_receipts

    env.case.remotes[1].completed(message_id)
    result = NativeRelayResult(conversation_id=member.conversation_id, request_id=member.request_id)
    returned = env.client.post(NATIVE_AGENT_PREFIX + "/result", headers=member_headers,
                              json=result.model_dump(mode="json"))
    assert returned.status_code == 202, returned.text
    assert returned.json()["message_id"] == message_id
    saved = env.client.get(path, headers=env.owner).json()
    assert [post["kind"] for post in saved["posts"]] == ["request", "result"]
    assert saved["deliveries"][0]["status"] == "completed"
