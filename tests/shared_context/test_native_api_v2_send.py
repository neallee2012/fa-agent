"""ADR-0009 M1 2d-i (P3): the API keeps SENDING foundry-native-v1, also into a conversation an H2d image labelled v2.

Until P9 every Hosted definition still declares v1, so every request byte stays what it was before P3: the digests
below were taken on ``origin/main`` 8bf86af0 (before P3) with the same fixed IDs. Offline doubles only."""

from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_native_responses_api import submit_native_turn, turn_profile
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NATIVE_TURN_HEADER, NativeConversation
from shared_context.native_continuation_wire import NativeContinuation
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NATIVE_STATE_PROFILE, NATIVE_V2_PROFILE
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_routine_api import daily_definition, routine_api  # noqa: F401
from test_native_turn_api import native_api, submit  # noqa: F401
from test_native_turn_wire import agent_receive, capture

AGENT = "11111111-1111-1111-1111-111111111111"
# Taken on 8bf86af0 (before P3) with a v1-labelled conversation: every submit's (operation, x-client-fa-state-profile,
# body) the owner route sent, and the request DTO bytes submit_native_turn sent.
BASE_OWNER_REQUESTS = "bb2a0c0f9354dde666a4ac0d9bc2da459a843f0fa3a2a6c20eceedd770a89efd"
BASE_TURN_REQUESTS = "2f1e5f4fb257bbbfd6738b5a5dc4fe2e509dee0f6061bc25466c0e4324d54370"
# The routine's (operation, header, body), taken the same way.
BASE_ROUTINE = "5205e4875fc242bf2cdbc3683613834a75e3bd5fc5cee1fbf4a1388d479af7e3"


@pytest.fixture
def base_platform(monkeypatch):
    """The bases were taken on Windows; a new capture's default checkpoint profile names this process's platform."""
    monkeypatch.setattr("shared_context.behavior_sdk_state.local_platform", lambda: "win32-x64")


def conversation(profile, cid="native-cid", **changes):
    kept = capture()
    return NativeConversation(
        state_profile=profile, agent_id=AGENT, hosted_session_id="OFFLINE-host-session", conversation_id=cid,
        last_turn=1, last_request_id="rid-0", capture=kept, capture_digest=kept.sha256, **changes)


@pytest.mark.parametrize("label, sent", [
    (NATIVE_STATE_PROFILE, NATIVE_STATE_PROFILE), (NATIVE_V2_PROFILE, NATIVE_STATE_PROFILE),
    (NATIVE_RESPONSES_PROFILE, NATIVE_RESPONSES_PROFILE)])
def test_a_conversation_gives_its_channel_and_the_api_sends_its_own_profile(label, sent):
    """Routines (native_routines) and every submit_native_turn caller take the profile from here."""
    assert turn_profile(capture(), conversation=conversation(label)) == sent


def _requests(monkeypatch):
    sent = []

    def invoke(client, request):
        sent.append(request)
        return SimpleNamespace(status="accepted", dispatch=None), None

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(invoke))
    return sent


def _turns(label):
    """A follow-up, an Agent message and an official-recovery continuation, each into conversation ``label``."""
    held = conversation(label)
    follow_up = agent_receive(rid="message-2", cid="native-cid", recipient=AGENT)
    continuation = NativeContinuation(continuation_of="rid-0", root_request_id="rid-0", attempt=1,
                                      source_submit_digest="d" * 64, checkpoint_sha256="a" * 64)
    return [
        dict(command=InstructionSubmit(request_id="rid-1", conversation_id="native-cid", content="Original text"),
             capture=capture()),
        dict(command=follow_up.command, capture=follow_up.capture, source=follow_up.source),
        dict(command=InstructionSubmit(request_id="rid-0-continued", conversation_id="native-cid",
                                       content="Original text"), capture=capture(), continuation=continuation),
    ], held


def _digest(requests):
    return hashlib.sha256(b"\n".join(request.model_dump_json().encode("utf-8") for request in requests)).hexdigest()


@pytest.mark.parametrize("label", [NATIVE_STATE_PROFILE, NATIVE_V2_PROFILE])
def test_a_v2_labelled_conversation_is_sent_the_same_v1_bytes_as_before(monkeypatch, label):
    """Owner follow-up, Agent message (relay) and official-recovery continuation (also the Agent API's)."""
    client = SimpleNamespace(target=SimpleNamespace(agent_id=AGENT))
    requests = _requests(monkeypatch)
    turns, held = _turns(label)
    for turn in turns:
        # Since 34e4af98 an RID no receipt holds is start-pinned from composition.runtime first.
        submit_native_turn(SimpleNamespace(runtime=None), client, turn.pop("command"), turn.pop("capture"),
                           conversation=held, **turn)
    assert [request.state_profile for request in requests] == [NATIVE_STATE_PROFILE] * 3
    assert _digest(requests) == BASE_TURN_REQUESTS


def _labelled(case, monkeypatch, profile):
    """Hosted answers conversation reads with ``profile`` as an H2d image would label its own conversations."""
    remote = case.remote

    def route(request):
        response = remote(request)
        if json.loads(request.content)["operation"] == "conversation":
            body = {**json.loads(response.content), "state_profile": profile}
            return httpx.Response(response.status_code, json=body, headers={
                key: value for key, value in response.headers.items() if key.lower() != "content-length"})
        return response

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(route))


def _owner_requests(case):
    return [(json.loads(request.content)["operation"], request.headers[NATIVE_TURN_HEADER], request.content)
            for request, _ in case.remote.calls]


@pytest.mark.usefixtures("base_platform")
@pytest.mark.parametrize("label", [NATIVE_STATE_PROFILE, NATIVE_V2_PROFILE])
def test_an_owner_follow_up_into_a_v2_labelled_conversation_is_sent_as_v1(native_api, monkeypatch, label):  # noqa: F811
    case = native_api
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    _labelled(case, monkeypatch, label)
    follow_up = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert follow_up.status_code == 202, follow_up.text
    sent = _owner_requests(case)
    assert {header for _, header, _ in sent} == {NATIVE_STATE_PROFILE}
    submits = [json.loads(body) for operation, _, body in sent if operation == "submit"]
    assert len(submits) == 2 and all("state_profile" not in body for body in submits)
    # Only the submits: how many session reads precede them depends on reader caches other tests leave behind.
    digest = hashlib.sha256(b"\n".join(b"%s %s %s" % (operation.encode(), header.encode(), body)
                                       for operation, header, body in sent if operation == "submit")).hexdigest()
    assert digest == BASE_OWNER_REQUESTS


@pytest.mark.usefixtures("base_platform")
@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_a_routine_into_a_v2_labelled_conversation_is_sent_as_v1(routine_api, monkeypatch):  # noqa: F811
    case = routine_api
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=1)
    saved = case.client.post(case.routine_prefix + "/definitions", headers=case.headers, json=daily_definition(
        enabled=True, schedule={"kind": "daily", "time_zone": "UTC", "local_time": due.strftime("%H:%M")}))
    assert saved.status_code == 200, saved.text
    _labelled(case, monkeypatch, NATIVE_V2_PROFILE)
    case.c.h.time = due + timedelta(seconds=2)
    result = case.trigger({"routine_id": "daily-summary", "scheduled_for": due.isoformat()})
    assert result.status_code == 202, result.text
    sent = [(header, body) for operation, header, body in _owner_requests(case) if operation == "routine_receive"]
    assert len(sent) == 1 and sent[0][0] == NATIVE_STATE_PROFILE and "state_profile" not in json.loads(sent[0][1])
    assert hashlib.sha256(b"routine_receive %s %s" % (sent[0][0].encode(), sent[0][1])).hexdigest() == BASE_ROUTINE
