"""Verified Agent relay to native receive, with original HTTP transports and offline remote replies."""

from types import SimpleNamespace
from contextlib import contextmanager
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import InMemoryRepository
from shared_context.behavior_api import _install_error_handlers
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context import behavior_agent_versions, behavior_native_sessions
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_foundry import FoundryHttpClient
from shared_context.behavior_native_capture import resolve_capture
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeReceipt, NativeSessionPreparation, NativeSubmit
from shared_context.behavior_native_sessions import native_session_id, native_sessions
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.errors import Problem
from test_agent_runtime import peer_runtime
from test_conversation_transfer_startup import synthetic_standing_v1
from test_conversation_profile_selection import template
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_native_turn_api import Credential, NativeTransport
from test_agent_onboarding import team
from test_agent_runtime_routing import agent_headers


class AgentTransport(NativeTransport):
    def __call__(self, request):
        command = NATIVE_REQUEST.validate_json(request.content)
        if command.operation != "agent_receive":
            return super().__call__(request)
        self.calls.append((request, command))
        original = self.records.get(command.command.request_id)
        if original is not None and original.submit_digest != command.sha256:
            return httpx.Response(409, json={"error": {"code": "NATIVE_RID_CONFLICT"}})
        value = original or NativeReceipt(
            agent_id=self.target.agent_id, hosted_session_id=self.target.session_id,
            request_id=command.command.request_id, conversation_id=command.command.conversation_id or "peer-native-cid",
            command=command.command, capture=command.capture, source=command.source,
            submit_digest=command.sha256, capture_digest=command.capture.sha256, status="accepted")
        self.records[value.request_id] = value
        if self.lose_submit:
            raise httpx.ReadTimeout("synthetic accepted response lost", request=request)
        return httpx.Response(202, content=value.model_dump_json().encode(), headers={
            "content-type": "application/json", "x-agent-invocation-id": "original-platform-invocation",
            "x-agent-session-id": self.target.session_id})


@contextmanager
def relay_transports(case, monkeypatch, *, select_receiver):
    originals, clients, remotes = [], [], []
    for composition in (case.a, case.b):
        target = composition.runtime.dep.native_launches.targets[0]
        version = behavior_agent_versions.current_hosted_version(composition.runtime, target)
        target = target.model_copy(update={"session_id": native_session_id(
            target, composition.config.identity.managed_identity_client_id, version=version), "agent_version": version})
        remote = AgentTransport(target)
        remote.preparation = NativeSessionPreparation(
            agent_id=target.agent_id, request_id="fixture-native-prepared",
            hosted_session_id=target.session_id, hosted_agent_version=target.agent_version,
            previous_hosted_session_id=None)
        native_sessions(composition).prepared = remote.preparation
        client = FoundryHttpClient(target, Credential(), transport=httpx.MockTransport(remote))
        originals.append(composition.clients)
        composition.clients = tuple(item for item in composition.clients if not isinstance(item, FoundryHttpClient)) + (client,)
        clients.append(client)
        remotes.append(remote)
    if select_receiver:
        select_profile(case.b.runtime.work_policy, case.b.assistant.authority, case.c.human, SelectConversationProfile(
            request_id="choose-native-receiver", expected_selection_id=None, template=template("fa-instruction-v2")))
    source = case.a.config.agent_scope(case.a.config.targets[0].agent_id)
    principal = BehaviorPrincipal(
        str(source.tenant_id), str(source.identity.oid), source.team_id, "agent", source.identity.actor,
        frozenset({"read", "submit", "execute"}))
    original_command = InstructionSubmit(request_id="original-sender-rid", content="Ask the peer to check the sample.")
    request = NativeSubmit(command=original_command, capture=resolve_capture(
        case.a.runtime.work_policy, case.a.assistant.authority, case.c.human, original_command))
    record = NativeReceipt(
        agent_id=source.target.agent_id, hosted_session_id=clients[0].target.session_id,
        request_id=request.command.request_id, conversation_id="original-sender-cid",
        command=request.command, capture=request.capture,
        submit_digest=request.sha256, capture_digest=request.capture.sha256, status="started", turn_number=1)
    remotes[0].records[record.request_id] = record
    try:
        def platform_session(client):
            original_sid = native_session_id(
                client.target, case.a.config.identity.managed_identity_client_id, version=client.target.agent_version)
            return client.target if client.target.session_id == original_sid else None
        # The test's own monkeypatch: a test that replaces these again is undone first, then this original.
        # A separate patch() here was undone before the test's monkeypatch and left the fixture's fake behind.
        monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", platform_session)
        monkeypatch.setattr("shared_context.behavior_native_sessions._latest_version_reader", lambda *_: None)
        yield SimpleNamespace(base=case, principal=principal, sender=record,
                              remotes=remotes, clients=clients, repository=InMemoryRepository())
    finally:
        for composition, original, client in zip((case.a, case.b), originals, clients):
            composition.clients = original
            client.close()


@pytest.fixture
def relay_case(peer_runtime, monkeypatch):
    with relay_transports(peer_runtime, monkeypatch, select_receiver=True) as case:
        yield case


@pytest.fixture
def registry_relay_case(team, monkeypatch):
    finished, result = team.run("native-inbox-recipient", name="Researcher", text="Check the supplied public samples.")
    assert finished and result["state"] == "ready"
    peer = team.a.for_agent(result["agent_id"])
    base = SimpleNamespace(c=team.c, a=team.a, b=peer)
    with relay_transports(base, monkeypatch, select_receiver=False) as case:
        yield case


@pytest.fixture
def chain_relay_case(team, monkeypatch):
    _, first = team.run("native-chain-b", name="B", text="Delegate a public check if needed.")
    _, third = team.run("native-chain-c", name="C", text="Check the public sample.")
    b, c = team.a.for_agent(first["agent_id"]), team.a.for_agent(third["agent_id"])
    base = SimpleNamespace(c=team.c, a=team.a, b=b)
    target = c.runtime.dep.native_launches.targets[0]
    version = behavior_agent_versions.current_hosted_version(c.runtime, target)
    target = target.model_copy(update={"session_id": native_session_id(
        target, c.config.identity.managed_identity_client_id, version=version), "agent_version": version})
    remote = AgentTransport(target)
    remote.preparation = NativeSessionPreparation(
        agent_id=target.agent_id, request_id="fixture-child-prepared", previous_hosted_session_id=None,
        hosted_session_id=target.session_id, hosted_agent_version=target.agent_version)
    native_sessions(c).prepared = remote.preparation
    client = FoundryHttpClient(target, Credential(), transport=httpx.MockTransport(remote))
    old = c.clients
    c.clients = tuple(item for item in old if not isinstance(item, FoundryHttpClient)) + (client,)
    try:
        with relay_transports(base, monkeypatch, select_receiver=False) as case:
            case.third, case.third_remote = c, remote
            yield case
    finally:
        c.clients = old
        client.close()


@pytest.fixture
def relay_http(request, signing_key):
    from shared_context.native_agent_api import install_native_agent_routes
    loop = asyncio.new_event_loop()
    try:
        case = request.getfixturevalue("relay_case")
        app = FastAPI()
        _install_error_handlers(app)
        install_native_agent_routes(app, case.base.a, enabled=True, repository=case.repository)
        with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
            yield case, client, agent_headers(case.base.c, signing_key, 0), case.base.c.h.headers(signing_key)
    finally:
        if not loop.is_closed():
            loop.close()


@pytest.fixture
def relay_client_http(request):
    with asyncio.Runner() as runner:
        yield runner, request.getfixturevalue("relay_http")


def test_native_agent_relay_returns_only_the_original_durable_acceptance_without_registering_a_table_job(relay_case):
    from shared_context.native_agent_api import NativeAgentRelayGateway, NativeRelaySend

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = NativeRelaySend(
        conversation_id=case.sender.conversation_id, request_id=case.sender.request_id,
        tool_call_id="original-sdk-send", message={
            "recipient_agent_id": case.base.b.agent_id, "kind": "request", "content": "Check this public sample."})
    before = case.base.a.runtime.store._guard()
    receipt = gateway.send(case.principal, command)
    assert receipt.recipient_agent_id == case.base.b.agent_id and receipt.status == "accepted"
    assert len(case.remotes[1].records) == 1
    assert gateway.send(case.principal, command) == receipt
    assert len(case.remotes[1].records) == 1
    assert case.base.a.runtime.work_policy.records == case.base.b.runtime.work_policy.records == {}
    assert case.base.a.runtime.store._guard() == before
    assert len(case.repository.data) == 1


def send_command(case):
    from shared_context.native_agent_api import NativeRelaySend
    return NativeRelaySend(
        conversation_id=case.sender.conversation_id, request_id=case.sender.request_id,
        tool_call_id="original-sdk-send", message={
            "recipient_agent_id": case.base.b.agent_id, "kind": "request", "content": "Check the public sample."})


@pytest.mark.parametrize("missing", [False, True], ids=["different-call", "missing-original"])
def test_saved_sender_ack_never_authorizes_a_new_or_recreated_delivery(relay_case, missing):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    accepted = gateway.send(case.principal, command)
    case.remotes[0].records[case.sender.request_id] = case.sender.model_copy(update={"delegation": accepted})
    if missing:
        case.remotes[1].records.pop(accepted.message_id)
    else:
        command = command.model_copy(update={"tool_call_id": "another-original-call"})
    before = sum(command.operation == "agent_receive" for _, command in case.remotes[1].calls)
    with pytest.raises(Problem) as refused:
        gateway.send(case.principal, command)
    assert refused.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert sum(command.operation == "agent_receive" for _, command in case.remotes[1].calls) == before
    assert case.remotes[0].records[case.sender.request_id].delegation == accepted


@pytest.mark.parametrize("field,value", [
    ("message_id", "another-message"), ("recipient_agent_id", "another-peer"),
    ("conversation_id", "another-peer-cid"), ("submit_digest", "f" * 64),
    ("source_conversation_id", "another-source-cid"), ("source_request_id", "another-source-rid"),
    ("state_profile", "foundry-responses-v1"),
])
def test_saved_ack_scope_is_checked_before_dispatching_an_existing_message(relay_case, field, value):
    from shared_context.native_agent_api import NativeAgentRelayGateway
    from shared_context.native_responses_wire import NativeResponseDispatch

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    accepted = gateway.send(case.principal, command)
    changes = {field: value}
    if field == "state_profile":
        changes["dispatch"] = NativeResponseDispatch(state="needs_response", inflight_request_id=accepted.message_id)
    wrong = accepted.model_copy(update=changes)
    case.remotes[0].records[case.sender.request_id] = case.sender.model_copy(update={"delegation": wrong})
    before = sum(command.operation == "agent_receive" for _, command in case.remotes[1].calls)
    with pytest.raises(Problem) as refused:
        gateway.send(case.principal, command)
    assert refused.value.body["error"]["code"] == "NATIVE_RELAY_OUTCOME_UNKNOWN"
    assert sum(command.operation == "agent_receive" for _, command in case.remotes[1].calls) == before


@pytest.mark.parametrize("status", ["started", "unknown"])
def test_known_sender_ack_allows_only_its_original_receiver_dispatch(relay_case, status):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    accepted = gateway.send(case.principal, command)
    sender = case.sender.model_copy(update={
        "status": status, "delegation": accepted,
        "failure_code": "NATIVE_TURN_UNKNOWN" if status == "unknown" else None})
    case.remotes[0].records[case.sender.request_id] = sender
    assert gateway.send(case.principal, command) == accepted
    assert list(case.remotes[1].records) == [accepted.message_id]
    assert case.remotes[0].records[case.sender.request_id] == sender


@pytest.mark.parametrize("status", ["completed", "unknown"])
@pytest.mark.parametrize("change", [None, "call", "content", "cancelled"])
def test_only_the_recorded_queued_command_can_send_after_the_sender_turn_ends(relay_case, status, change):
    from shared_context.native_agent_api import NativeAgentRelayGateway
    from shared_context.native_relay_delivery import reference_for

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    case.remotes[0].completed(case.sender.request_id)
    sender = case.remotes[0].records[case.sender.request_id]
    sender = sender.model_copy(update={
        "queued_relay": reference_for(sender, command), "status": status,
        "result": sender.result if status == "completed" else None,
        "failure_code": "NATIVE_TURN_UNKNOWN" if status == "unknown" else None,
        "cancel_requested": change == "cancelled"})
    case.remotes[0].records[case.sender.request_id] = sender
    if change == "call":
        command = command.model_copy(update={"tool_call_id": "different-call"})
    elif change == "content":
        command = command.model_copy(update={"message": command.message.model_copy(update={"content": "Different intent."})})
    if change is None:
        accepted = gateway.send(case.principal, command)
        assert accepted.status == "accepted"
        assert len(case.remotes[1].records) == 1
    else:
        with pytest.raises(Problem) as refused:
            gateway.send(case.principal, command)
        assert refused.value.body["error"]["code"] == "NATIVE_AGENT_SOURCE_INVALID"
        assert not case.remotes[1].records
    assert case.remotes[0].records[case.sender.request_id] == sender


def hide_receiver_session(case, monkeypatch):
    # The receiver's selector moved without a reader yet; a newly started API holds no cached binding.
    native_sessions(case.base.b).forget()
    read = behavior_native_sessions._read_platform_session
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", lambda client: (
        None if client.target.agent_id == case.base.b.agent_id else read(client)))


def test_http_relay_preserves_following_refusal_for_the_official_sender_retry(relay_http):
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView

    case, client, headers, _ = relay_http
    target = case.clients[1].target
    case.remotes[1].following = NativeEnvironmentView(
        agent_id=target.agent_id, request_id="follow-peer",
        previous_hosted_session_id=target.session_id, previous_agent_version=target.agent_version,
        hosted_session_id="offline-following-peer", hosted_agent_version="42",
        state="pending", reason="accepted_or_started")
    response = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers,
                           json=send_command(case).model_dump(mode="json"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING"
    assert case.remotes[1].records == {} and not case.repository.data


def test_original_sender_drives_a_stale_recipient_to_ready_without_a_human_or_portal(relay_http, monkeypatch):
    from shared_context.behavior_native_agent_wire import NATIVE_AGENT_PREFIX
    from test_native_environment_api import environment_at

    case, client, headers, human = relay_http
    selected, read = behavior_agent_versions.current_hosted_version, behavior_native_sessions._read_platform_session
    original_http = case.clients[1].http._transport
    source, calls, sessions, views = environment_at(
        SimpleNamespace(remote=case.remotes[1], foundry=case.clients[1]), monkeypatch)
    peer_read = behavior_native_sessions._read_platform_session
    monkeypatch.setattr(behavior_agent_versions, "current_hosted_version",
                        lambda runtime, target, **kwargs: "42" if target.agent_id == case.base.b.agent_id
                        else selected(runtime, target, **kwargs))
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session",
                        lambda target: peer_read(target) if target.target.agent_id == case.base.b.agent_id else read(target))
    environment_http = case.clients[1].http._transport

    def remote(packet):
        command = NATIVE_REQUEST.validate_json(packet.content)
        if command.operation in {"read", "agent_receive"}:
            case.remotes[1].target = sessions[packet.url.params["agent_session_id"]]
            return original_http.handle_request(packet)
        return environment_http.handle_request(packet)

    monkeypatch.setattr(case.clients[1].http, "_transport", httpx.MockTransport(remote))
    body = send_command(case).model_dump(mode="json")
    first = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=body)
    assert first.status_code == 503 and first.json()["error"]["code"] == "AGENT_VERSION_FOLLOWING", first.text
    assert not case.remotes[1].records and not case.repository.data
    denied = client.post(NATIVE_AGENT_PREFIX + "/environment", headers=human, json=body)
    assert denied.status_code == 403
    ready = client.post(NATIVE_AGENT_PREFIX + "/environment", headers=headers, json=body)
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    assert ready.json()["model_started"] is False and case.remotes[1].records == {}
    repeated = client.post(NATIVE_AGENT_PREFIX + "/environment", headers=headers, json=body)
    assert repeated.json() == ready.json()
    accepted = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=body)
    assert accepted.status_code == 202, accepted.text
    again = client.post(NATIVE_AGENT_PREFIX + "/send", headers=headers, json=body)
    assert again.json() == accepted.json()
    assert len(case.remotes[1].records) == len(case.repository.data) == 1
    assert calls.count(("stop", source.session_id)) == 1
    assert len(sessions) == 2 and views[0].state == "ready"


def test_lost_receive_response_keeps_original_id_and_only_one_quota_entry(relay_case):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    case.remotes[1].lose_submit = True
    with pytest.raises(Problem) as unknown:
        gateway.send(case.principal, command)
    assert unknown.value.body["error"]["code"] == "FOUNDRY_INVOCATION_UNKNOWN"
    assert len(case.remotes[1].records) == len(case.repository.data) == 1
    case.remotes[1].lose_submit = False
    received = gateway.send(case.principal, command)
    assert len(case.remotes[1].records) == 1
    original = case.remotes[1].records[received.message_id]
    assert gateway._quota(case.principal).read(original.source.root_id).messages[0].message_id == received.message_id


@pytest.mark.parametrize("side", [0, 1], ids=["sender", "recipient"])
@pytest.mark.parametrize("previous", [False, True], ids=["first-attempt", "retry-after-lost-ack"])
def test_native_preflight_distinguishes_recipient_from_sender_and_preserves_prior_delivery(relay_case, side, previous):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    if previous:
        case.remotes[1].lose_submit = True
        with pytest.raises(Problem) as unknown:
            gateway.send(case.principal, command)
        assert unknown.value.body["error"]["code"] == "FOUNDRY_INVOCATION_UNKNOWN"
        case.remotes[1].lose_submit = False
    receipts = dict(case.remotes[1].records)
    quota = dict(case.repository.data)
    case.remotes[side].error = (403, "NATIVE_TURNS_DISABLED")
    with pytest.raises(Problem) as refused:
        gateway.send(case.principal, command)
    assert refused.value.body["error"]["code"] == (
        "NATIVE_RELAY_RECIPIENT_UNAVAILABLE" if side == 1 else "NATIVE_TURNS_DISABLED")
    assert case.remotes[1].records == receipts and case.repository.data == quota
    assert len(receipts) == len(quota) == int(previous)
    assert sum(command.operation == "agent_receive" for _, command in case.remotes[1].calls) == int(previous)


@pytest.mark.parametrize("unavailable", ["unprepared", "no-transport"])
def test_unavailable_receiver_is_refused_without_creating_a_session_or_quota(relay_case, monkeypatch, unavailable):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    if unavailable == "unprepared":
        native_sessions(case.base.b).prepared = None
    else:
        case.base.b.clients = tuple(client for client in case.base.b.clients if not isinstance(client, FoundryHttpClient))
    hide_receiver_session(case, monkeypatch)
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    with pytest.raises(Problem) as refused:
        gateway.send(case.principal, send_command(case))
    assert refused.value.body["error"]["code"] == (
        "AGENT_VERSION_FOLLOWING" if unavailable == "unprepared" else "NATIVE_RELAY_RECIPIENT_UNAVAILABLE")
    assert not case.repository.data and not case.remotes[1].records
    assert not native_sessions(case.base.b).environment_creates


def test_native_directory_keeps_ready_recipients_with_unknown_native_cache_without_extra_lookup(relay_case, monkeypatch):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    native_sessions(case.base.b).prepared = None

    def unexpected_lookup(_client):
        pytest.fail("the directory must not probe Foundry for each recipient")

    monkeypatch.setattr("shared_context.behavior_native_sessions._read_platform_session", unexpected_lookup)
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    directory = gateway.directory(case.principal)
    assert case.base.b.agent_id in {entry.agent_id for entry in directory.agents}
    assert not any(remote.calls for remote in case.remotes)
    assert not case.repository.data and not native_sessions(case.base.b).environment_creates


def test_saved_result_relays_once_to_the_original_cid_and_large_results_remain_readable(relay_case):
    from dataclasses import replace
    from shared_context.native_agent_api import NativeAgentRelayGateway, NativeRelayReadResult, NativeRelayResult

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    accepted = gateway.send(case.principal, send_command(case))
    case.remotes[0].completed(case.sender.request_id)
    case.remotes[1].completed(accepted.message_id)
    sender = case.remotes[1].records[accepted.message_id]
    peer_scope = case.base.b.config.agent_scope(case.base.b.agent_id)
    peer = replace(case.principal, oid=str(peer_scope.identity.oid), actor_id=peer_scope.identity.actor)
    notify = NativeRelayResult(conversation_id=sender.conversation_id, request_id=sender.request_id)
    wake = gateway.result(peer, notify)
    assert wake.message_id == accepted.message_id
    assert wake.recipient_agent_id == case.sender.agent_id and wake.conversation_id == case.sender.conversation_id
    original = case.remotes[0].records[wake.message_id]
    assert original.source.result == sender.result and original.source.kind == "agent_result"
    assert original.command.content != case.remotes[1].text and "not the result text" in original.command.content
    assert gateway.result(peer, notify) == wake
    assert len(case.remotes[0].records) == 2
    page = gateway.read_result(case.principal, NativeRelayReadResult(
        conversation_id=wake.conversation_id, request_id=wake.message_id, part=0))
    assert page.result == sender.result and page.next_part == 1
    last = gateway.read_result(case.principal, NativeRelayReadResult(
        conversation_id=wake.conversation_id, request_id=wake.message_id, part=1))
    assert page.text + last.text == case.remotes[1].text
    case.remotes[1].text = "A different claimed result."
    case.remotes[1].completed(sender.request_id)
    with pytest.raises(Problem) as changed:
        gateway.result(peer, notify)
    assert changed.value.body["error"]["code"] == "NATIVE_RID_CONFLICT"
    assert case.remotes[0].records[wake.message_id] == original


def test_background_delivery_after_sender_completion_returns_the_reply_to_that_original_conversation(relay_case):
    from dataclasses import replace
    from shared_context.native_agent_api import NativeAgentRelayGateway, NativeRelayResult
    from shared_context.native_relay_delivery import reference_for

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    case.remotes[0].completed(case.sender.request_id)
    original = case.remotes[0].records[case.sender.request_id].model_copy(update={
        "queued_relay": reference_for(case.sender, command)})
    case.remotes[0].records[case.sender.request_id] = original
    accepted = gateway.send(case.principal, command)
    assert accepted.status == "accepted"
    case.remotes[1].completed(accepted.message_id)
    sender = case.remotes[1].records[accepted.message_id]
    peer_scope = case.base.b.config.agent_scope(case.base.b.agent_id)
    peer = replace(case.principal, oid=str(peer_scope.identity.oid), actor_id=peer_scope.identity.actor)
    notify = NativeRelayResult(conversation_id=sender.conversation_id, request_id=sender.request_id)
    wake = gateway.result(peer, notify)
    assert wake.conversation_id == original.conversation_id and wake.recipient_agent_id == original.agent_id
    assert wake.message_id == accepted.message_id
    assert gateway.result(peer, notify) == wake
    reply = case.remotes[0].records[wake.message_id]
    assert reply.source.continuation.request_id == original.request_id
    assert reply.source.result == sender.result and len(case.remotes[0].records) == 2
    assert case.remotes[0].records[case.sender.request_id] == original


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize("resumed", [False, True], ids=["original", "continuation"])
def test_a_pending_sender_environment_does_not_block_its_recorded_background_delivery(
    relay_case, monkeypatch, recorded, resumed,
):
    from shared_context.native_agent_api import NativeAgentRelayGateway
    from shared_context.native_relay_delivery import reference_for

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    command = send_command(case)
    case.remotes[0].completed(case.sender.request_id)
    sender = case.remotes[0].records[case.sender.request_id]
    if resumed:
        from shared_context.behavior_native_turn_wire import NativeContinuationSubmit, continuation_request_id
        from shared_context.native_continuation_wire import NativeContinuation

        parent = sender.model_copy(update={"status": "unknown", "result": None, "failure_code": "NATIVE_TURN_UNKNOWN"})
        rid = continuation_request_id(parent.agent_id, parent.conversation_id, parent.request_id, 1)
        submit = NativeContinuationSubmit(
            command=parent.command.model_copy(update={"request_id": rid, "conversation_id": parent.conversation_id}),
            capture=parent.capture, continuation=NativeContinuation(
                continuation_of=parent.request_id, root_request_id=parent.request_id, attempt=1,
                source_submit_digest=parent.submit_digest, checkpoint_sha256="b" * 64))
        sender = sender.model_copy(update={"request_id": rid, "command": submit.command,
                                          "continuation": submit.continuation, "submit_digest": submit.sha256})
        case.remotes[0].records[parent.request_id] = parent
        command = command.model_copy(update={"request_id": rid})
    if recorded:
        sender = sender.model_copy(update={"queued_relay": reference_for(sender, command)})
    case.remotes[0].records[sender.request_id] = sender
    client = gateway._client
    reader = case.clients[0].for_target(case.clients[0].target.model_copy(update={
        "session_id": "OFFLINE-new-pending-reader", "agent_version": "99"}))
    case.remotes[0].target = reader.target

    def pending(scope, *, prepared=True, **kwargs):
        if scope.target.agent_id == sender.agent_id and prepared:
            raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE new sender environment is waiting for this task")
        return client(scope, prepared=prepared, **kwargs)

    monkeypatch.setattr(gateway, "_client", pending)
    monkeypatch.setattr(native_sessions(case.base.a), "reader", lambda _client: reader)
    if recorded:
        result = gateway.send(case.principal, command)
        assert result.status == "accepted" and len(case.remotes[1].records) == 1
    else:
        with pytest.raises(Problem) as denied:
            gateway.send(case.principal, command)
        assert denied.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"
        assert not case.remotes[1].records
    assert case.remotes[0].records[sender.request_id] == sender


def test_onboarding_registry_agent_is_a_ready_native_recipient_without_changing_legacy_routing(registry_relay_case):
    from shared_context.native_agent_api import NativeAgentRelayGateway
    from shared_context.behavior_agent_onboarding import NEW_AGENT_PROFILE

    case = registry_relay_case
    assert case.base.b.agent_id not in {target.agent_id for target in case.base.a.config.targets}
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    directory = gateway.directory(case.principal)
    assert case.base.b.agent_id in {agent.agent_id for agent in directory.agents}
    accepted = gateway.send(case.principal, send_command(case))
    received = case.remotes[1].records[accepted.message_id]
    assert received.capture.template.role.profile == NEW_AGENT_PROFILE
    assert received.capture.role_prompt.text == "Check the supplied public samples."
    assert received.source.recipient_agent_id == case.base.b.agent_id
    assert received.command.authorize_own_result is False
    assert case.base.b.runtime.work_policy.records == {}


def test_signed_agent_http_entry_never_accepts_a_human_or_model_supplied_authority(relay_http):
    from shared_context.native_agent_api import PREFIX

    case, client, agent, human = relay_http
    command = send_command(case).model_dump(mode="json")
    denied = client.post(PREFIX + "/send", json=command, headers=human)
    assert denied.status_code == 403 and not case.repository.data
    forged = client.post(PREFIX + "/send", json={**command, "root_id": "model-chosen-root"}, headers=agent)
    assert forged.status_code == 422 and not case.repository.data
    accepted = client.post(PREFIX + "/send", json=command, headers=agent)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["recipient_agent_id"] == case.base.b.agent_id
    assert "capture" not in accepted.json()


@pytest.mark.parametrize("failure,previous", [
    ("recipient-disabled", False), ("recipient-disabled", True),
    ("recipient-unprepared", False), ("recipient-no-transport", False),
    ("sender-disabled", False), ("lost-ack", False),
], ids=["recipient-disabled", "recipient-after-lost-ack", "recipient-unprepared",
        "recipient-no-transport", "sender-disabled", "lost-ack"])
def test_native_recipient_feedback_survives_the_real_api_error_handler(relay_client_http, monkeypatch, failure, previous):
    from azure.core.exceptions import AzureError
    from azure.ai.agentserver.core.tasks import Task
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.behavior_native_agent_wire import NativeRelayAcceptance
    from shared_context.native_agent_api import PREFIX
    from shared_context.native_agent_tools import NativeAgentRelayClient, _WaitingEnvironment

    runner, (case, api, agent, _human) = relay_client_http
    command = send_command(case)
    if previous:
        case.remotes[1].lose_submit = True
        first = api.post(PREFIX + "/send", json=command.model_dump(mode="json"), headers=agent)
        assert first.status_code == 503
        case.remotes[1].lose_submit = False
    receipts, quota = dict(case.remotes[1].records), dict(case.repository.data)
    if failure == "recipient-disabled":
        case.remotes[1].error = (403, "NATIVE_TURNS_DISABLED")
    elif failure == "recipient-unprepared":
        native_sessions(case.base.b).prepared = None
        hide_receiver_session(case, monkeypatch)
    elif failure == "recipient-no-transport":
        case.base.b.clients = tuple(client for client in case.base.b.clients if not isinstance(client, FoundryHttpClient))
    elif failure == "sender-disabled":
        case.remotes[0].error = (403, "NATIVE_TURNS_DISABLED")
    else:
        case.remotes[1].lose_submit = True
    responses, follow_inputs = [], []

    async def unavailable_follow(_task, **options):
        follow_inputs.append(options["input"])
        raise AzureError("OFFLINE Hosted task registration unavailable")

    # This fixture owns the HTTP boundary, not a Hosted task manager.
    monkeypatch.setattr(Task, "start", unavailable_follow)

    def transport(request):
        response = api.request(request.method, request.url.path, content=request.content, headers=agent)
        responses.append(response)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    async def token():
        return "synthetic-relay-token"

    hosted = NativeAgentRelayClient("https://api.invalid", token, transport=httpx.MockTransport(transport))
    with pytest.raises(Problem) as refused:
        runner.run(hosted.send(case.sender, command.tool_call_id, command.message))
    code = refused.value.body["error"]["code"]
    assert follow_inputs == ([command] if failure == "lost-ack" else
                             [_WaitingEnvironment(command=command)] if failure == "recipient-unprepared" else [])
    boundary = AssistantModelBoundary(None, text_reply=True, capability_results={
        "send_agent_message": NativeRelayAcceptance})
    feedback = boundary.public_tool_failure("send_agent_message", code)
    assert len(responses) == 1 and responses[0].headers["Cache-Control"] == "no-store"
    assert "private upstream details" not in responses[0].text
    if failure == "recipient-unprepared":
        assert code == "AGENT_VERSION_FOLLOWING"
        assert responses[0].json()["error"]["accepted"] is False
        assert responses[0].json()["error"]["automatic_retry"] is False
        assert not receipts and not quota
    elif failure.startswith("recipient-"):
        assert code == "NATIVE_RELAY_RECIPIENT_UNAVAILABLE"
        assert responses[0].json()["error"]["automatic_retry"] is False
        assert responses[0].json()["error"]["reconcile_original"] is True
        assert "對方目前無法接收" in feedback and "這次沒有新增投遞" in feedback
        assert "先前" in feedback and "未能確認" in feedback and "不要換 ID 重送" in feedback
        assert "Authority refused" not in feedback
    elif failure == "sender-disabled":
        assert code == "NATIVE_RELAY_REFUSED" and "對方目前無法接收" not in feedback
    else:
        assert code == "NATIVE_RELAY_OUTCOME_UNKNOWN" and "對方目前無法接收" not in feedback
    if failure != "lost-ack":
        assert case.remotes[1].records == receipts and case.repository.data == quota
        assert len(receipts) == len(quota) == int(previous)
    else:
        assert len(case.remotes[1].records) == len(case.repository.data) == 1


def test_unknown_peer_outcome_wakes_a_once_without_claiming_the_sdk_stopped(relay_case):
    from dataclasses import replace
    from shared_context.native_agent_api import NativeAgentRelayGateway, NativeRelayResult

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    accepted = gateway.send(case.principal, send_command(case))
    case.remotes[0].completed(case.sender.request_id)
    original = case.remotes[1].records[accepted.message_id]
    unknown = NativeReceipt.model_validate({
        **original.model_dump(), "status": "unknown", "turn_number": 1, "failure_code": "NATIVE_TURN_TIMEOUT"})
    case.remotes[1].records[unknown.request_id] = unknown
    peer_scope = case.base.b.config.agent_scope(case.base.b.agent_id)
    peer = replace(case.principal, oid=str(peer_scope.identity.oid), actor_id=peer_scope.identity.actor)
    query = NativeRelayResult(conversation_id=unknown.conversation_id, request_id=unknown.request_id)
    notified = gateway.outcome(peer, query)
    stored = case.remotes[0].records[notified.message_id]
    assert notified.conversation_id == case.sender.conversation_id and notified.message_id == accepted.message_id
    assert stored.source.kind == "agent_notice" and stored.source.outcome.failure_code == "NATIVE_TURN_TIMEOUT"
    assert stored.source.result is None and "未確認" in stored.command.content
    assert "不表示" in stored.command.content and "停止" in stored.command.content
    assert gateway.outcome(peer, query) == notified
    assert len(case.remotes[0].records) == 2 and case.remotes[1].records[unknown.request_id] == unknown


def test_human_read_of_original_a_surfaces_child_notification_exhaustion_without_rewriting_it(relay_case):
    from shared_context.native_agent_api import NativeAgentRelayGateway
    from shared_context.behavior_native_turn_api import NativeTurnGateway
    from shared_context.behavior_native_turn_wire import NativeRead

    case = relay_case
    relay = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    delegated = relay.send(case.principal, send_command(case))
    case.remotes[0].completed(case.sender.request_id)
    parent = NativeReceipt.model_validate({
        **case.remotes[0].records[case.sender.request_id].model_dump(), "delegation": delegated})
    case.remotes[0].records[parent.request_id] = parent
    case.remotes[1].completed(delegated.message_id)
    child = NativeReceipt.model_validate({
        **case.remotes[1].records[delegated.message_id].model_dump(), "notification_error": "NATIVE_POST_TURN_UNKNOWN"})
    case.remotes[1].records[child.request_id] = child
    visible, _ = NativeTurnGateway(case.base.a, enabled=True).read(
        case.base.c.human, NativeRead(request_id=parent.request_id))
    assert visible.notification_error == "NATIVE_POST_TURN_UNKNOWN"
    assert visible.result == parent.result and visible.command == parent.command
    assert case.remotes[0].records[parent.request_id] == parent


@pytest.mark.parametrize("b_cancelled", [False, True], ids=["completed-handoff", "cancelled-after-handoff"])
def test_nested_delegation_does_not_consume_as_result_and_final_returns_to_the_original_human_cid(
    chain_relay_case, b_cancelled,
):
    from dataclasses import replace
    from shared_context.native_agent_api import NativeAgentRelayGateway, NativeRelayResult, NativeRelaySend
    from shared_context.behavior_team_registry import resolve_agent_scope

    case = chain_relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    ab = gateway.send(case.principal, send_command(case))
    case.remotes[0].completed(case.sender.request_id)
    original_b = case.remotes[1].records[ab.message_id]
    case.remotes[1].records[ab.message_id] = NativeReceipt.model_validate({
        **original_b.model_dump(), "status": "started", "turn_number": 1})
    b_scope = resolve_agent_scope(case.base.a.runtime.store, case.base.a.config, case.base.b.agent_id)
    b = replace(case.principal, oid=str(b_scope.identity.oid), actor_id=b_scope.identity.actor)
    bc = gateway.send(b, NativeRelaySend(
        conversation_id=original_b.conversation_id, request_id=original_b.request_id,
        tool_call_id="b-to-c-original", message={
            "recipient_agent_id": case.third.agent_id, "kind": "request", "content": "Verify the original sample."}))
    case.remotes[1].text = "已交給 C，等待原结果。"
    case.remotes[1].completed(ab.message_id)
    delegated = NativeReceipt.model_validate({
        **case.remotes[1].records[ab.message_id].model_dump(), "delegation": bc,
        **({"status": "unknown", "result": None, "failure_code": "NATIVE_TURN_CANCELLED", "cancel_requested": True}
           if b_cancelled else {})})
    case.remotes[1].records[ab.message_id] = delegated
    with pytest.raises(Problem) as interim:
        gateway.result(b, NativeRelayResult(conversation_id=delegated.conversation_id, request_id=delegated.request_id))
    assert interim.value.body["error"]["code"] == "AGENT_MESSAGE_REPLY_DENIED"
    if b_cancelled:
        with pytest.raises(Problem):
            gateway.outcome(b, NativeRelayResult(
                conversation_id=delegated.conversation_id, request_id=delegated.request_id))
    assert ab.message_id not in case.remotes[0].records

    case.third_remote.text = "The original check is 530."
    case.third_remote.completed(bc.message_id)
    c_scope = resolve_agent_scope(case.base.a.runtime.store, case.base.a.config, case.third.agent_id)
    c = replace(case.principal, oid=str(c_scope.identity.oid), actor_id=c_scope.identity.actor)
    completed_c = case.third_remote.records[bc.message_id]
    returned = gateway.result(c, NativeRelayResult(
        conversation_id=completed_c.conversation_id, request_id=completed_c.request_id))
    wake_b = case.remotes[1].records[returned.message_id]
    assert wake_b.source.continuation.request_id == delegated.request_id
    case.remotes[1].text = "The final reviewed answer is 530."
    case.remotes[1].completed(wake_b.request_id)
    completed_b = case.remotes[1].records[wake_b.request_id]
    final = gateway.result(b, NativeRelayResult(
        conversation_id=completed_b.conversation_id, request_id=completed_b.request_id))
    wake_a = case.remotes[0].records[final.message_id]
    assert final.message_id == ab.message_id and final.conversation_id == case.sender.conversation_id
    assert wake_a.source.sender.request_id == completed_b.request_id
    assert wake_a.source.result == completed_b.result and wake_a.source.in_reply_to == ab.message_id
    assert wake_a.source.result != delegated.result


def test_a_reply_from_another_conversation_is_refused_and_the_real_one_returns_to_the_original_cid(relay_case):
    """B1 PA-01: a peer result is accepted only for its original request and CID; nothing is written otherwise."""
    from dataclasses import replace
    from shared_context.native_agent_api import NativeAgentRelayGateway, NativeRelayResult

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    accepted = gateway.send(case.principal, send_command(case))
    case.remotes[0].completed(case.sender.request_id)
    case.remotes[1].completed(accepted.message_id)
    child = case.remotes[1].records[accepted.message_id]
    peer_scope = case.base.b.config.agent_scope(case.base.b.agent_id)
    peer = replace(case.principal, oid=str(peer_scope.identity.oid), actor_id=peer_scope.identity.actor)
    before = (dict(case.remotes[0].records), dict(case.repository.data))
    for claimed in (
        NativeRelayResult(conversation_id="another-peer-cid", request_id=child.request_id),
        NativeRelayResult(conversation_id=case.sender.conversation_id, request_id=child.request_id),
        NativeRelayResult(conversation_id=case.sender.conversation_id, request_id=case.sender.request_id),
    ):
        with pytest.raises(Problem) as refused:
            gateway.result(peer, claimed)
        assert refused.value.body["error"]["code"] == "AGENT_MESSAGE_REPLY_DENIED"
        assert (case.remotes[0].records, case.repository.data) == before
    wake = gateway.result(peer, NativeRelayResult(conversation_id=child.conversation_id, request_id=child.request_id))
    assert wake.conversation_id == case.sender.conversation_id and wake.recipient_agent_id == case.sender.agent_id
    returned = case.remotes[0].records[wake.message_id]
    assert returned.command.conversation_id == case.sender.conversation_id
    assert returned.source.kind == "agent_result" and returned.source.in_reply_to == accepted.message_id


def test_a_relay_send_reads_the_recipient_selector_and_never_invokes_its_kept_old_session(relay_case, monkeypatch):
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    assert gateway.send(case.principal, send_command(case)).status == "accepted"
    assert native_sessions(case.base.b).reader_cached()
    # Another process moved the recipient's selector to 99 just now; nobody created 99's session yet.
    selected, read = behavior_agent_versions.current_hosted_version, behavior_native_sessions._read_platform_session
    monkeypatch.setattr(behavior_agent_versions, "current_hosted_version", lambda runtime, target, **kwargs: (
        "99" if target.agent_id == case.base.b.agent_id else selected(runtime, target, **kwargs)))
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", lambda client: (
        None if client.target.agent_version == "99" else read(client)))
    before = len(case.remotes[1].calls)
    with pytest.raises(Problem) as refused:
        gateway.send(case.principal, send_command(case))
    assert (refused.value.status, refused.value.body["error"]["code"]) == (503, "AGENT_VERSION_FOLLOWING")
    assert len(case.remotes[1].calls) == before


def test_a_continuation_reads_the_sender_selector_and_never_invokes_its_kept_old_session(relay_case, monkeypatch):
    from shared_context.behavior_native_agent_wire import NativeRelayResult
    from shared_context.native_agent_api import NativeAgentRelayGateway

    case = relay_case
    gateway = NativeAgentRelayGateway(case.base.a, enabled=True, repository=case.repository)
    assert gateway.send(case.principal, send_command(case)).status == "accepted"
    assert native_sessions(case.base.a).reader_cached()
    # Another process moved the sender's selector to 99 just now; nobody created 99's session yet.
    selected, read = behavior_agent_versions.current_hosted_version, behavior_native_sessions._read_platform_session
    monkeypatch.setattr(behavior_agent_versions, "current_hosted_version", lambda runtime, target, **kwargs: (
        "99" if target.agent_id == case.sender.agent_id else selected(runtime, target, **kwargs)))
    monkeypatch.setattr(behavior_native_sessions, "_read_platform_session", lambda client: (
        None if client.target.agent_version == "99" else read(client)))
    before = len(case.remotes[0].calls)
    with pytest.raises(Problem) as refused:
        gateway.continue_original(case.principal, NativeRelayResult(
            conversation_id=case.sender.conversation_id, request_id=case.sender.request_id))
    assert (refused.value.status, refused.value.body["error"]["code"]) == (409, "NATIVE_SESSION_CHANGED")
    assert len(case.remotes[0].calls) == before
