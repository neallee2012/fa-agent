"""Signed peer-message seams; no model, Hosted process, or network."""

from copy import deepcopy
from types import MappingProxyType, SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from shared_context.behavior_capabilities import available_grants
from shared_context.behavior_profile_selection import SelectConversationProfile, select_profile
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_peer_runtime import install_peer_routing
from shared_context.behavior_work_policy_api import install_work_policy_routes
from test_agent_runtime import peer_runtime
from test_agent_runtime_routing import agent_headers, routing_peer
from test_conversation_transfer_startup import claimed_turn, read_startup, synthetic_standing_v1
from test_conversation_profile_selection import template
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_behavior_store import store
from hosted_versions_stub import deployed_versions


def application(composition):
    app = create_assistant_app(composition.assistant.authority, composition.verifier,
                               provider=composition.assistant.provider)
    install_work_policy_routes(app, composition)
    return app


def test_signed_directory_lists_registered_agents_without_exposing_credentials_or_starting_work(
        routing_peer, signing_key):
    peers, loop = routing_peer
    c, a, b = peers.c, peers.a, peers.b
    app = application(a)
    install_peer_routing(app, a, application)
    before = a.runtime.store._guard()
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        response = client.get("/work-policy/v1/agents", headers=c.h.headers(signing_key, "Behavior.Read"))
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["schema_version"] == "fa-agent-directory-v1"
        assert value["default_agent_id"] == c.h.config.targets[0].agent_id
        assert [item["agent_id"] for item in value["agents"]] == [
            target.agent_id for target in c.h.config.targets
        ]
        assert value["agents"][1]["agent_id"] == b.agent_id
        for item in value["agents"]:
            assert set(item) == {"agent_id", "display_name", "api_prefix", "state", "reason"}
            assert item["api_prefix"] == "/agents/" + item["agent_id"]
        for item in value["agents"][:2]:
            assert item["state"] == "ready" and item["reason"] is None
        for item in value["agents"][2:]:
            assert item["state"] == "unavailable" and item["reason"] == "AGENT_SCOPE_MISSING"
        assert client.get("/work-policy/v1/agents").status_code == 401
        assert client.get("/work-policy/v1/agents",
                          headers=agent_headers(c, signing_key, 0)).status_code == 403
    assert a.runtime.store._guard() == before
    assert c.sessions.calls == []


def test_unavailable_b_stays_explicit_without_becoming_the_default_agent(routing_peer, signing_key, monkeypatch):
    peers, loop = routing_peer
    original = peers.c.h.config.targets[0]
    deployed_versions(monkeypatch, {original.agent_id: original.agent_version})
    with TestClient(application(peers.a), backend_options={"loop_factory": lambda: loop}) as client:
        result = client.get("/work-policy/v1/agents", headers=peers.c.h.headers(signing_key, "Behavior.Read"))
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["default_agent_id"] == peers.c.h.config.targets[0].agent_id
    assert value["agents"][0]["state"] == "ready"
    assert value["agents"][1]["agent_id"] == peers.b.agent_id
    assert value["agents"][1]["state"] == "unavailable"
    assert value["agents"][1]["reason"] == "AGENT_VERSION_FOLLOWING"


@pytest.fixture
def message_turn(routing_peer, signing_key, request):
    peers, loop = routing_peer
    c, a, b = peers.c, peers.a, peers.b
    grants = available_grants(None, ("fa-agent-collaboration-v1",))
    a.runtime.capability_grants = grants
    responses = getattr(request, "param", None) == "v2-responses"
    select_profile(c.control, c.authority, c.human, SelectConversationProfile(
        request_id="select-peer-collaboration", expected_selection_id=None,
        template=template("fa-instruction-v2" if responses else "fa-instruction-history-v1",
                          user_text_file_profile="fa-user-text-files-v1",
                          capabilities=(grants["fa-agent-collaboration-v1"].ref,),
                          **({"model_provider": "openai-responses"} if responses else {}))))
    turn = claimed_turn(c, "original-human-peer-root")
    app = application(a)
    install_peer_routing(app, a, application)
    path = (f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant"
            f"/sessions/{turn.assignment.session_id}/agent-message-send")
    headers = {**agent_headers(c, signing_key, 0),
               "x-behavior-assistant-ticket": turn.ticket.get_secret_value()}
    command = {"request_id": "agent-message-send", "message": {
        "message_id": "one-original-handoff", "recipient_agent_id": b.agent_id,
        "kind": "handoff", "content": "Compare the two supplied synthetic totals.", "refs": [],
    }}
    with TestClient(app, backend_options={"loop_factory": lambda: loop}) as client:
        yield SimpleNamespace(c=c, a=a, b=b, turn=turn, path=path, headers=headers, command=command,
                              client=client, human_headers=c.h.headers(signing_key, "Behavior.Read"))


@pytest.fixture
def shared_message_turn(request, monkeypatch):
    import test_agent_runtime
    from shared_context.behavior_work_policy import SteadyWorkPolicy
    from shared_context.source_materials import digest
    original = test_agent_runtime.peer_policy
    def shared_policy(config, policy):
        peer = original(config, policy)
        binding = peer.template.model_copy(update={"source_ids": policy.template.source_ids})
        profile = peer.profile.model_copy(update={"source": policy.profile.source})
        runtime_policy = peer.legacy_template_policy.model_copy(update={
            "binding_digest": digest(["fa-behavior-api-v1", peer.evidence_mode, binding.model_dump(mode="json")])})
        return SteadyWorkPolicy.model_validate_json(peer.model_copy(update={
            "template": binding, "profile": profile, "legacy_template_policy": runtime_policy}).model_dump_json())
    monkeypatch.setattr(test_agent_runtime, "peer_policy", shared_policy)
    return request.getfixturevalue("message_turn")


@pytest.mark.parametrize("message_turn", [None, "v2-responses"], indirect=True)
def test_original_signed_assignment_records_one_handoff_and_replays_only_its_receipt(message_turn, monkeypatch):
    m = message_turn
    c, a, turn, client = m.c, m.a, m.turn, m.client
    path, command, headers = m.path, m.command, m.headers
    assert turn.assignment.agent_message_recipient.model_dump() == {
        "agent_id": m.b.agent_id, "display_name": c.h.config.targets[1].agent_name,
    }
    legacy = turn.assignment.model_copy(update={"agent_message_recipient": None}).model_dump_json()
    assert "agent_message_recipient" not in legacy
    assert type(turn.assignment).model_validate_json(legacy).model_dump_json() == legacy
    from shared_context.behavior_agent_message_tools import agent_message_capability
    tools, _ = agent_message_capability(SimpleNamespace(client=SimpleNamespace(assignment=turn.assignment)),
                                       a.runtime.capability_grants["fa-agent-collaboration-v1"])
    assert tools[0].parameters["properties"]["recipient_agent_id"]["enum"] == [m.b.agent_id]
    first = client.post(path, json=command, headers=headers)
    assert first.status_code == 200, first.text
    value = first.json()
    message = value["message"]
    assert value["fresh"] is True
    assert message["sender_agent_id"] == c.h.config.targets[0].agent_id
    assert message["recipient_agent_id"] == m.b.agent_id
    assert message["from_work_id"] == turn.binding.work.work_id
    assert message["to_work_id"] != message["from_work_id"]
    assert message["from_conversation_id"] == turn.assignment.task.instruction.conversation_id
    assert message["to_conversation_id"] != message["from_conversation_id"]
    assert message["hop"] == 1 and message["status"] == "queued"
    assert message["decision"]["model_dispatch_authorized"] is False
    assert message["human_obligation_fulfilled"] is False
    before = a.runtime.store._guard()
    replay = client.post(path, json=command, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**value, "fresh": False}
    assert a.runtime.store._guard() == before
    original = client.post(path.removesuffix("send") + "read", json={
        "request_id": "agent-message-read", "message_key": message["message_key"],
    }, headers=headers)
    assert original.status_code == 200, original.text
    assert original.json() == replay.json()
    projection = client.get(f"/work-policy/v1/bindings/{turn.binding.binding_id}/agent-messages",
                            headers=m.human_headers)
    assert projection.status_code == 200, projection.text
    assert projection.json()["messages"] == [message]
    assert "content" not in projection.text and "sender_oid" not in projection.text
    m.b.runtime.work_policy.refresh()
    assert len(m.b.runtime.work_policy.records) == 1
    receiver = next(iter(m.b.runtime.work_policy.records.values()))
    assert receiver.binding.work.work_id == message["to_work_id"]
    assert receiver.principal_oid == turn.binding.agent_oid
    assert receiver.profile.instruction.peer_work is not None
    assert receiver.profile.memory_scope is None and receiver.profile.instruction.capabilities is None
    assert receiver.binding.source_ids == m.b.runtime.work_policy.policy.template.source_ids
    from shared_context.behavior_auth import BehaviorPrincipal
    actor = c.h.config.identity.auth().actors[receiver.binding.agent_oid]
    reader = BehaviorPrincipal(receiver.binding.tenant_id, actor.oid, actor.team, "agent", actor.actor,
                               frozenset({"read"}))
    assert m.b.runtime.authorize(receiver.binding.binding_id, reader, "read")[0] == receiver.binding
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_workers import worker_key
    absent = (worker_key("job", receiver.binding.job_id),
              control_key("delivery-obligation", receiver.binding.obligation_id))
    assert all(row is None for row in a.runtime.store.read_snapshot(absent).values())
    before_submit = a.runtime.store._guard()
    with monkeypatch.context() as cold:
        cold.setattr(m.b.runtime, "profiles", MappingProxyType({
            key: value for key, value in m.b.runtime.profiles.items() if key != receiver.binding.binding_id}))
        wrong_submit = client.post(
            f"/agents/{m.b.agent_id}/behavior/v1/bindings/{receiver.binding.binding_id}/assistant/submit",
            json={"request_id": "not-the-original-peer-consumer"},
            headers=m.human_headers)
    assert wrong_submit.status_code == 403, wrong_submit.text
    assert a.runtime.store._guard() == before_submit
    b_projection = client.get(
        f"/agents/{m.b.agent_id}/work-policy/v1/bindings/{receiver.binding.binding_id}/agent-messages",
        headers=m.human_headers)
    assert b_projection.status_code == 200, b_projection.text
    assert b_projection.json()["agent_id"] == m.b.agent_id
    assert b_projection.json()["messages"] == [message]
    wrong_prefix = client.get(
        f"/agents/{c.h.config.targets[0].agent_id}/work-policy/v1/bindings/{receiver.binding.binding_id}/agent-messages",
        headers=m.human_headers)
    assert wrong_prefix.status_code == 403
    assert a.runtime.job_status(turn.binding.binding_id, c.human).obligation_fulfilled is False
    assert c.sessions.calls == []
    read_startup(c)


@pytest.mark.parametrize(("changed", "expected"), [
    ({"source_kind": "human"}, 422),
    ({"sender_agent_id": "agent-b"}, 422),
    ({"root_request_id": "foreign-root"}, 422),
    ({"recipient_agent_id": "unknown-agent"}, 403),
    ({"in_reply_to": "foreign-message"}, 403),
])
def test_untrusted_message_fields_are_rejected_before_any_peer_record(message_turn, changed, expected):
    m = message_turn
    command = deepcopy(m.command)
    command["message"].update(changed)
    before = m.a.runtime.store._guard()
    response = m.client.post(m.path, json=command, headers=m.headers)
    assert response.status_code == expected, response.text
    assert m.a.runtime.store._guard() == before
    assert m.c.sessions.calls == []


def test_same_message_id_with_different_content_cannot_replace_the_original(message_turn):
    m = message_turn
    first = m.client.post(m.path, json=m.command, headers=m.headers)
    assert first.status_code == 200, first.text
    changed = deepcopy(m.command)
    changed["message"]["content"] = "Replace the retained original."
    before = m.a.runtime.store._guard()
    response = m.client.post(m.path, json=changed, headers=m.headers)
    assert response.status_code == 409, response.text
    assert m.a.runtime.store._guard() == before
    restored = m.client.post(m.path.removesuffix("send") + "read", json={
        "request_id": "agent-message-read", "message_key": first.json()["message"]["message_key"],
    }, headers=m.headers)
    assert restored.status_code == 200 and restored.json()["content"] == m.command["message"]["content"]


def test_signed_handoff_uses_both_registered_source_readers_and_current_revocation(shared_message_turn):
    m = shared_message_turn
    source = m.a.runtime.profiles[m.turn.binding.binding_id].source
    reference = {"visibility": "team", "resource_kind": "source", "tenant_id": m.turn.binding.tenant_id,
                 "team_id": m.turn.binding.team_id, "resource_id": source.source_id,
                 "version": str(source.version), "sha256": source.sha256}
    command = deepcopy(m.command)
    command["message"]["refs"] = [reference]
    denied = deepcopy(command)
    denied["message"]["refs"][0]["team_id"] = "not-the-original-team"
    wrong = m.client.post(m.path, json=denied, headers=m.headers)
    assert wrong.status_code == 403, wrong.text
    assert m.b.runtime.work_policy.records == {}
    accepted = m.client.post(m.path, json=command, headers=m.headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["refs"] == [reference]
    key = accepted.json()["message"]["message_key"]
    read = {"request_id": "agent-message-read", "message_key": key}
    assert m.client.post(m.path.removesuffix("send") + "read", json=read, headers=m.headers).status_code == 200
    m.b.runtime.work_policy.revoke(m.c.human, "peer-read-revoked")
    before = m.a.runtime.store._guard()
    refused = m.client.post(m.path.removesuffix("send") + "read", json=read, headers=m.headers)
    assert refused.status_code == 403, refused.text
    assert m.a.runtime.store._guard() == before
    assert m.c.sessions.calls == []


def test_root_references_cannot_expand_the_original_receiver_source_scope(message_turn):
    m = message_turn
    source = m.a.runtime.profiles[m.turn.binding.binding_id].source
    command = deepcopy(m.command)
    command["message"]["refs"] = [{
        "visibility": "team", "resource_kind": "source", "tenant_id": m.turn.binding.tenant_id,
        "team_id": m.turn.binding.team_id, "resource_id": source.source_id,
        "version": str(source.version), "sha256": source.sha256,
    }]
    assert source.source_id not in m.b.runtime.work_policy.policy.template.source_ids
    refused = m.client.post(m.path, json=command, headers=m.headers)
    assert refused.status_code == 403, refused.text
    assert m.b.runtime.work_policy.records == {}
    assert m.c.sessions.calls == []


def test_sender_source_refusal_does_not_reserve_the_receiver_conversation(shared_message_turn, monkeypatch):
    from shared_context.errors import Problem
    m = shared_message_turn
    source = m.a.runtime.profiles[m.turn.binding.binding_id].source
    command = deepcopy(m.command)
    command["message"]["refs"] = [{
        "visibility": "team", "resource_kind": "source", "tenant_id": m.turn.binding.tenant_id,
        "team_id": m.turn.binding.team_id, "resource_id": source.source_id,
        "version": str(source.version), "sha256": source.sha256,
    }]
    def changed(*args, **kwargs):
        raise Problem(409, "AGENT_MESSAGE_REFS_CHANGED", "Injected source refusal at the real sender reader")
    with monkeypatch.context() as fault:
        fault.setattr(m.a.runtime, "read_source", changed)
        refused = m.client.post(m.path, json=command, headers=m.headers)
        assert refused.status_code == 409, refused.text
    assert m.b.runtime.work_policy.records == {}
    corrected = deepcopy(m.command)
    corrected["message"]["message_id"] = "corrected-handoff-before-acceptance"
    accepted = m.client.post(m.path, json=corrected, headers=m.headers)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["message"]["status"] == "queued"


def test_exhausted_message_allowance_does_not_reserve_the_receiver_conversation(message_turn):
    m = message_turn
    for index in range(7):
        message = deepcopy(m.command)
        message["message"].update(message_id=f"fyi-{index}", kind="fyi")
        saved = m.client.post(m.path, json=message, headers=m.headers)
        assert saved.status_code == 200, saved.text
    refused = m.client.post(m.path, json=m.command, headers=m.headers)
    assert refused.status_code == 429, refused.text
    assert m.b.runtime.work_policy.records == {}


@pytest.mark.parametrize("after_commit", [False, True])
def test_original_message_transaction_conflict_does_not_strand_the_receiver(message_turn, monkeypatch, after_commit):
    from shared_context.behavior_agent_messages import message_key
    from shared_context.errors import Problem
    m = message_turn
    registration = m.a.runtime.work_policy.records[m.turn.binding.binding_id]
    identifier = message_key(registration.admission_id, m.c.h.config.targets[0].agent_id,
                             m.command["message"]["message_id"])
    transact = m.a.runtime.store.transact
    calls = []
    def conflict(request, decide, **kwargs):
        if request.request_id == identifier:
            calls.append(request.request_id)
            if len(calls) == 1:
                if after_commit:
                    transact(request, decide, **kwargs)
                raise Problem(412, "BEHAVIOR_CONFLICT", "Injected original message commit conflict")
        return transact(request, decide, **kwargs)
    monkeypatch.setattr(m.a.runtime.store, "transact", conflict)
    saved = m.client.post(m.path, json=m.command, headers=m.headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["message"]["message_key"] == identifier
    assert saved.json()["fresh"] is (not after_commit)
    assert len(calls) == (1 if after_commit else 2)
    assert len(m.b.runtime.work_policy.records) == 1 and m.c.sessions.calls == []
    read_startup(m.c)


def test_unaccepted_peer_registration_does_not_make_b_busy_and_same_input_can_reconcile(
        shared_message_turn, monkeypatch):
    from shared_context.behavior_agent_message_api import _receiver_busy
    from shared_context.errors import Problem
    m = shared_message_turn
    source = m.a.runtime.profiles[m.turn.binding.binding_id].source
    command = deepcopy(m.command)
    command["message"]["refs"] = [{
        "visibility": "team", "resource_kind": "source", "tenant_id": m.turn.binding.tenant_id,
        "team_id": m.turn.binding.team_id, "resource_id": source.source_id,
        "version": str(source.version), "sha256": source.sha256,
    }]
    def changed(*args, **kwargs):
        raise Problem(409, "SOURCE_EXPIRED", "Injected expiry after sender preflight")
    with monkeypatch.context() as fault:
        fault.setattr(m.b.runtime, "read_source", changed)
        refused = m.client.post(m.path, json=command, headers=m.headers)
        assert refused.status_code == 409, refused.text
    assert len(m.b.runtime.work_policy.records) == 1
    original = next(iter(m.b.runtime.work_policy.records.values())).model_dump_json()
    assert _receiver_busy(m.b, None) is False
    recovered = m.client.post(m.path, json=command, headers=m.headers)
    assert recovered.status_code == 200, recovered.text
    assert next(iter(m.b.runtime.work_policy.records.values())).model_dump_json() == original
    assert m.c.sessions.calls == []


def test_receiver_registration_without_a_job_keeps_the_new_handoff_queued(message_turn):
    import hashlib
    from shared_context.behavior_assistant_wire import InstructionSubmit
    m = message_turn
    original = InstructionSubmit(request_id="b-admission-not-yet-finished", content="Synthetic pending B work.")
    m.b.runtime.work_policy.register(m.c.human, original.request_id,
        hashlib.sha256(original.model_dump_json().encode()).hexdigest(), instruction=original)
    result = m.client.post(m.path, json=m.command, headers=m.headers)
    assert result.status_code == 200, result.text
    assert result.json()["message"]["status"] == "queued"
    assert result.json()["message"]["decision"]["reason"] == "original_receiver_busy"
    assert result.json()["message"]["decision"]["model_dispatch_authorized"] is False
    assert m.c.sessions.calls == []


def test_message_listing_does_not_read_every_other_registered_turn(message_turn, monkeypatch):
    import hashlib
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from test_assistant_images import _read_bytes
    m = message_turn
    path = f"/work-policy/v1/bindings/{m.turn.binding.binding_id}/agent-messages"
    first, used = _read_bytes(monkeypatch, lambda: m.client.get(path, headers=m.human_headers))
    assert first.status_code == 200, first.text
    for number in range(3):
        other = InstructionSubmit(request_id=f"a-other-work-{number}", content="Synthetic other A work.")
        m.a.runtime.work_policy.register(m.c.human, other.request_id,
            hashlib.sha256(other.model_dump_json().encode()).hexdigest(), instruction=other)
    store = m.a.runtime.store
    store._scope = store._scope.model_copy(update={"max_batch_bytes": used + 16384})
    listing = m.client.get(path, headers=m.human_headers)
    assert listing.status_code == 200, listing.text


def test_expired_first_handoff_is_refused_without_creating_a_root(message_turn, monkeypatch):
    from datetime import timedelta
    m = message_turn
    now = m.a.runtime._now() + timedelta(seconds=m.a.runtime.work_policy.policy.queue_seconds + 1)
    monkeypatch.setattr(m.a.runtime, "clock", lambda: now)
    before = m.a.runtime.store._guard()
    refused = m.client.post(m.path, json=m.command, headers=m.headers)
    assert refused.status_code == 403, refused.text
    assert m.a.runtime.store._guard() == before
    listing = m.client.get(f"/work-policy/v1/bindings/{m.turn.binding.binding_id}/agent-messages",
                           headers=m.human_headers)
    assert listing.status_code == 200 and listing.json()["messages"] == []


def test_uninitialized_message_ledger_keeps_the_existing_root_readable(message_turn, monkeypatch):
    from shared_context.behavior_agent_messages import AgentMessageLedger
    from shared_context.errors import Problem
    m = message_turn
    def unavailable(*args, **kwargs):
        raise Problem(503, "AGENT_MESSAGE_UNKNOWN", "Injected failure before ledger initialization")
    with monkeypatch.context() as fault:
        fault.setattr(AgentMessageLedger, "register_root", unavailable)
        refused = m.client.post(m.path, json=m.command, headers=m.headers)
        assert refused.status_code == 503, refused.text
    before = m.a.runtime.store._guard()
    listing = m.client.get(f"/work-policy/v1/bindings/{m.turn.binding.binding_id}/agent-messages",
                           headers=m.human_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["messages"] == []
    assert m.a.runtime.store._guard() == before
    read_startup(m.c)


@pytest.mark.parametrize("retained", [98, 99, 197])
def test_receiver_busy_respects_the_real_store_predicate_bound(store, retained, monkeypatch):
    from shared_context.behavior_agent_message_api import _receiver_busy
    adapter, _ = store
    records = {str(index): SimpleNamespace(profile=SimpleNamespace(instruction=True),
               binding=SimpleNamespace(job_id=f"original-job-{index}")) for index in range(retained)}
    control = SimpleNamespace(refresh=lambda: None, records=records)
    peer = SimpleNamespace(runtime=SimpleNamespace(work_policy=control, store=adapter))
    calls = []
    original = adapter.read_snapshot
    def read(keys):
        calls.append(len(keys))
        return original(keys)
    monkeypatch.setattr(adapter, "read_snapshot", read)
    assert _receiver_busy(peer, None) is True
    assert calls and max(calls) <= 98


def test_hosted_capability_registration_uses_the_bound_peer_tool_factory():
    from shared_context.behavior_assistant_hosted import CAPABILITY_HANDLERS
    from shared_context.behavior_agent_message_tools import agent_message_capability
    assert CAPABILITY_HANDLERS["fa-agent-collaboration-v1"] is agent_message_capability


def test_informational_message_silence_keeps_the_original_human_obligation(message_turn):
    m = message_turn
    command = deepcopy(m.command)
    command["message"]["kind"] = "fyi"
    first = m.client.post(m.path, json=command, headers=m.headers)
    assert first.status_code == 200, first.text
    assert first.json()["message"]["status"] == "silenced"
    assert first.json()["message"]["decision"]["action"] == "silence"
    before = m.a.runtime.store._guard()
    again = m.client.post(m.path, json=command, headers=m.headers)
    assert again.status_code == 200 and again.json()["fresh"] is False
    assert m.a.runtime.store._guard() == before
    assert m.a.runtime.job_status(m.turn.binding.binding_id, m.c.human).obligation_fulfilled is False
    assert m.c.sessions.calls == []
    read_startup(m.c)


def test_peer_message_requires_the_original_agent_ticket_and_binding(message_turn, signing_key):
    m = message_turn
    before = m.a.runtime.store._guard()
    for headers in (
        {**m.headers, "x-behavior-assistant-ticket": "f" * 64},
        {**m.headers, **m.human_headers},
        {**m.headers, **agent_headers(m.c, signing_key, 1)},
    ):
        refused = m.client.post(m.path, json=m.command, headers=headers)
        assert refused.status_code == 403, refused.text
    assert m.a.runtime.store._guard() == before


def test_complete_large_fyi_uses_the_same_inline_message_limit_in_both_directions(message_turn):
    m = message_turn
    command = deepcopy(m.command)
    command["message"].update(kind="fyi", content="x" * 30000)
    result = m.client.post(m.path, json=command, headers=m.headers)
    assert result.status_code == 200, result.text
    assert result.json()["content"] == command["message"]["content"]
    assert result.json()["message"]["status"] == "silenced"
    original = m.client.post(m.path.removesuffix("send") + "read", json={
        "request_id": "agent-message-read", "message_key": result.json()["message"]["message_key"],
    }, headers=m.headers)
    assert original.status_code == 200, original.text
    assert original.json()["content"] == command["message"]["content"]


def test_selected_collaboration_without_handoff_allows_the_ninth_human_model_request(message_turn):
    m = message_turn
    path = m.path.removesuffix("agent-message-send") + "reserve-model"
    for number in range(1, 9):
        request_id = f"{number:064x}"
        result = m.client.post(path, json={"request_id": request_id, "request_sha256": request_id}, headers=m.headers)
        assert result.status_code == 200, result.text
    request_id = f"{9:064x}"
    accepted = m.client.post(path, json={"request_id": request_id, "request_sha256": request_id}, headers=m.headers)
    assert accepted.status_code == 200, accepted.text
    assert m.b.runtime.work_policy.records == {}
    assert m.c.sessions.calls == []


def test_lost_send_response_is_recovered_by_read_without_client_reposting(message_turn, monkeypatch):
    import json
    import httpx
    from shared_context import behavior_assistant_api as routes
    from shared_context.behavior_agent_messages import AgentMessage
    from shared_context.behavior_agent_message_wire import AgentMessageRead
    from shared_context.behavior_assistant_hosted import AssistantClient
    from shared_context.errors import Problem

    m = message_turn
    original = routes._response
    lost = []

    def lose_response(value, **kwargs):
        if isinstance(value, AgentMessageRead) and value.fresh and not lost:
            lost.append(value.message.message_key)
            raise Problem(503, "AGENT_MESSAGE_RESPONSE_LOST", "Injected loss after the original save")
        return original(value, **kwargs)

    monkeypatch.setattr(routes, "_response", lose_response)
    calls = []

    def forward(request):
        calls.append(request.url.path)
        response = m.client.request(request.method, request.url.path, content=request.content, headers=request.headers)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    client = AssistantClient("https://synthetic-api.invalid", m.turn.assignment, m.turn.ticket,
        token_provider=lambda: m.headers["Authorization"].removeprefix("Bearer "), transport=httpx.MockTransport(forward))
    try:
        with pytest.raises(Problem):
            client.send_agent_message(AgentMessage.model_validate_json(json.dumps(m.command["message"])))
        assert calls == [m.path] and len(lost) == 1
        before = m.a.runtime.store._guard()
        projection = m.client.get(f"/work-policy/v1/bindings/{m.turn.binding.binding_id}/agent-messages",
                                  headers=m.human_headers)
        assert projection.status_code == 200, projection.text
        key = projection.json()["messages"][0]["message_key"]
        restored = client.read_agent_message(key)
        assert key == lost[0] and restored.content == m.command["message"]["content"]
        assert restored.fresh is False and restored.message.status == "queued"
        assert calls.count(m.path) == 1 and m.a.runtime.store._guard() == before
        assert m.c.sessions.calls == []
    finally:
        client.close()
    read_startup(m.c)


def test_rejected_send_preserves_a_safe_reason_from_api_through_hosted_without_recording_private_values(
        message_turn, caplog):
    import httpx
    from shared_context.behavior_agent_message_wire import AgentMessage
    from shared_context.behavior_assistant_hosted import AssistantClient
    from shared_context.errors import Problem

    m = message_turn
    responses = []

    def forward(request):
        response = m.client.request(request.method, request.url.path, content=request.content, headers=request.headers)
        responses.append(response)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    client = AssistantClient("https://synthetic-api.invalid", m.turn.assignment, m.turn.ticket,
        token_provider=lambda: m.headers["Authorization"].removeprefix("Bearer "), transport=httpx.MockTransport(forward))
    before = m.a.runtime.store._guard()
    try:
        with pytest.raises(Problem) as refused:
            client.send_agent_message(AgentMessage(
                message_id="wrong-recipient", recipient_agent_id="B", kind="handoff",
                content="PRIVATE_BRIEF_MUST_NOT_APPEAR_IN_DIAGNOSTICS"))
    finally:
        client.close()
    assert len(responses) == 1
    assert responses[0].status_code == 403
    assert responses[0].headers["x-behavior-assistant-error-code"] == "AGENT_MESSAGE_ROUTE_DENIED"
    assert refused.value.body["error"]["code"] == "AGENT_MESSAGE_ROUTE_DENIED"
    assert any("BEHAVIOR_REQUEST_FAILED" in record.message and "AGENT_MESSAGE_ROUTE_DENIED" in record.message
               for record in caplog.records)
    assert any("AGENT_MESSAGE_REQUEST_FAILED" in record.message and "AGENT_MESSAGE_ROUTE_DENIED" in record.message
               for record in caplog.records)
    for private in ("PRIVATE_BRIEF_MUST_NOT_APPEAR_IN_DIAGNOSTICS",
                    m.headers["Authorization"].removeprefix("Bearer "), m.turn.ticket.get_secret_value()):
        assert private not in caplog.text and private not in str(refused.value.body)
    assert m.a.runtime.store._guard() == before
    assert m.b.runtime.work_policy.records == {} and m.c.sessions.calls == []


@pytest.mark.parametrize("message_turn", [None, "v2-responses"], indirect=True)
def test_b_registry_rejection_logs_original_code_before_masking_public_feedback(message_turn, monkeypatch, caplog):
    from shared_context import behavior_agent_registry
    from shared_context.errors import Problem
    m = message_turn
    original = behavior_agent_registry.registered_agent_scope
    checked = []

    def invalid(store, policy):
        if policy.agent_id == m.b.agent_id:
            checked.append(policy.agent_id)
            raise Problem(409, "AGENT_SCOPE_INVALID", "PRIVATE_REGISTRY_DIAGNOSTIC")
        return original(store, policy)

    monkeypatch.setattr(behavior_agent_registry, "registered_agent_scope", invalid)
    before = m.a.runtime.store._guard()
    refused = m.client.post(m.path, json=m.command, headers=m.headers)
    assert refused.status_code == 409 and checked == [m.b.agent_id]
    assert refused.headers["x-behavior-assistant-error-code"] == "UNCLASSIFIED"
    assert any("BEHAVIOR_REQUEST_FAILED" in record.message and "AGENT_SCOPE_INVALID" in record.message
               for record in caplog.records)
    assert "PRIVATE_REGISTRY_DIAGNOSTIC" not in caplog.text and "PRIVATE_REGISTRY_DIAGNOSTIC" not in refused.text
    assert m.a.runtime.store._guard() == before
    assert m.b.runtime.work_policy.records == {} and m.c.sessions.calls == []
