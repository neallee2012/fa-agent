"""ADR-0009 M1 2d-ii: the API SENDS foundry-native-v2, also into a v1-labelled or unlabeled conversation.

The header and every new request body carry v2 (the body label is written, as only v1 is left out); an original keeps
its own profile, so a same-RID resend of v1 material is still v1. Offline doubles only."""

from datetime import timedelta
import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_native_responses_api import submit_native_turn, turn_profile
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NATIVE_TURN_HEADER, NativeConversation
from shared_context.native_continuation_wire import NativeContinuation
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NATIVE_V2_PROFILE, UNLABELED_STATE_PROFILE
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_routine_api import daily_definition, routine_api  # noqa: F401
from test_native_turn_api import native_api, submit  # noqa: F401
from test_native_turn_wire import agent_receive, capture

AGENT = "11111111-1111-1111-1111-111111111111"
V1, V2 = UNLABELED_STATE_PROFILE, NATIVE_V2_PROFILE


def conversation(profile, cid="native-cid", **changes):
    kept = capture()
    labelled = {} if profile is None else {"state_profile": profile}  # None: an unlabeled (v1-era) conversation
    return NativeConversation(
        **labelled, agent_id=AGENT, hosted_session_id="OFFLINE-host-session", conversation_id=cid,
        last_turn=1, last_request_id="rid-0", capture=kept, capture_digest=kept.sha256, **changes)


@pytest.mark.parametrize("label, sent", [
    (None, V2), (V1, V2), (V2, V2), (NATIVE_RESPONSES_PROFILE, NATIVE_RESPONSES_PROFILE)])
def test_a_conversation_gives_its_channel_and_the_api_sends_its_own_profile(label, sent):
    """Routines (native_routines) and every submit_native_turn caller take the profile from here."""
    assert turn_profile(capture(), conversation=conversation(label)) == sent
    assert conversation(None).state_profile == V1  # an unlabeled conversation still reads as v1


def test_a_new_conversation_sends_v2_and_an_original_keeps_its_profile():
    """The template names the lane (unlabeled v1 = Invocations), never the profile sent."""
    assert capture().template.settings.state_profile == V1 and turn_profile(capture()) == V2
    original = SimpleNamespace(state_profile=V1)
    assert turn_profile(capture(), original=original, conversation=conversation(V2)) == V1


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


@pytest.mark.parametrize("label", [None, V1, V2])
def test_every_turn_into_a_v1_unlabeled_or_v2_conversation_is_sent_as_v2(monkeypatch, label):
    """Owner follow-up, Agent message (relay) and official-recovery continuation (also the Agent API's)."""
    client = SimpleNamespace(target=SimpleNamespace(agent_id=AGENT))
    requests = _requests(monkeypatch)
    turns, held = _turns(label)
    for turn in turns:
        # Since 34e4af98 an RID no receipt holds is start-pinned from composition.runtime first.
        submit_native_turn(SimpleNamespace(runtime=None), client, turn.pop("command"), turn.pop("capture"),
                           conversation=held, **turn)
    assert [request.state_profile for request in requests] == [V2] * 3
    assert all(json.loads(request.model_dump_json())["state_profile"] == V2 for request in requests)


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


@pytest.mark.parametrize("label", [V1, V2])
def test_an_owner_follow_up_into_a_v1_or_v2_conversation_is_sent_as_v2(native_api, monkeypatch, label):  # noqa: F811
    case = native_api
    assert submit(case).status_code == 202
    case.remote.completed("native-rid")
    _labelled(case, monkeypatch, label)
    follow_up = submit(case, rid="native-rid-2", conversation_id="native-cid")
    assert follow_up.status_code == 202, follow_up.text
    sent = _owner_requests(case)
    assert {header for _, header, _ in sent} == {V2}
    submits = [json.loads(body) for operation, _, body in sent if operation == "submit"]
    assert len(submits) == 2 and all(body["state_profile"] == V2 for body in submits)


@pytest.mark.parametrize("routine_api", ["clock"], indirect=True)
def test_a_routine_into_a_v2_labelled_conversation_is_sent_as_v2(routine_api, monkeypatch):  # noqa: F811
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
    assert len(sent) == 1 and sent[0][0] == V2 and json.loads(sent[0][1])["state_profile"] == V2


@pytest.mark.parametrize("value, enabled", [(None, False), (V1, True), (V2, True), ("foundry-native-v3", None)])
def test_the_apis_own_fa_native_turns_only_switches_native_on(monkeypatch, value, enabled):
    """2d-ii: the API's FA_NATIVE_TURNS (still the v1 string in the profiles) is a switch; it sends v2 either way."""
    from shared_context.behavior_native_turn_api import configured_native_turns
    from shared_context.errors import Problem

    if value is None:
        monkeypatch.delenv("FA_NATIVE_TURNS", raising=False)
    else:
        monkeypatch.setenv("FA_NATIVE_TURNS", value)
    if enabled is None:
        with pytest.raises(Problem):
            configured_native_turns()
    else:
        assert configured_native_turns() is enabled


def test_the_workbench_keeps_its_local_lane_label_for_v1_and_v2_receipts():
    """A v2 receipt runs on the same local Invocations lane (instruction_lanes CHECK and instructions.json keep v1)."""
    from web_test_console.instructions import NATIVE_LANE, lane_of

    assert NATIVE_LANE == V1 and lane_of(V1) == lane_of(V2) == NATIVE_LANE
    assert lane_of(NATIVE_RESPONSES_PROFILE) == NATIVE_RESPONSES_PROFILE


def _crashed_admission(case, monkeypatch):
    """Hosted after a crash between its admission mark and ``rid:<RID>``: ``admitting`` holds the digest of the first
    submit it saw, and a submit of the same RID with another digest is 409 ``NATIVE_RID_CONFLICT`` (native_turns
    ``_original_session``); the matching one is admitted (the double's create-or-read)."""
    remote, admitting, digests = case.remote, {}, []

    def route(request):
        command = json.loads(request.content)
        if command["operation"] != "submit":
            return remote(request)
        from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
        digest = NATIVE_REQUEST.validate_json(request.content).sha256
        digests.append(digest)
        if command["command"]["request_id"] not in admitting:
            admitting[command["command"]["request_id"]] = digest
            return httpx.Response(503, json={"error": {"code": "NATIVE_STATE_STORE_UNKNOWN", "message": "private"}})
        if admitting[command["command"]["request_id"]] != digest:
            return httpx.Response(409, json={"error": {"code": "NATIVE_RID_CONFLICT", "message": "private"}})
        return remote(request)

    monkeypatch.setattr(case.foundry.http, "_transport", httpx.MockTransport(route))
    return admitting, digests


def test_a_v1_admission_a_crashed_hosted_left_is_admitted_under_its_digest_on_resend(native_api, monkeypatch):  # noqa: F811
    """The API before 2d-ii sent v1; Hosted marked ``admitting`` and crashed before ``rid:<RID>``. The 2d-ii API's
    same-RID resend (v2) conflicts with that digest, so it resends the same material labelled v1 once: 202, receipt
    under the original digest, and the RID is admitted once."""
    from shared_context import behavior_native_responses_api as responses

    case = native_api
    admitting, digests = _crashed_admission(case, monkeypatch)
    real = responses.turn_profile
    monkeypatch.setattr(responses, "turn_profile", lambda capture, **kw: V1)  # the release-33 API sent v1
    assert submit(case).status_code == 503
    monkeypatch.setattr(responses, "turn_profile", real)
    again = submit(case)
    assert again.status_code == 202, again.text
    assert again.json()["submit_digest"] == admitting["native-rid"] and again.json()["state_profile"] == V1
    assert len(digests) == 3 and digests[1] != digests[0] == digests[2]  # v1 (crash), v2 (409), v1 again


@pytest.mark.parametrize("status, code", [(503, "NATIVE_STATE_STORE_UNKNOWN"), (504, "NATIVE_UPSTREAM_TIMEOUT")])
def test_an_unknown_answer_to_the_v1_resend_is_not_reported_as_a_conflict(monkeypatch, status, code):
    """AGENTS rule 7: only a second NATIVE_RID_CONFLICT says "not accepted"; a 503 or timeout stays retryable."""
    from shared_context.errors import Problem

    answers = [Problem(409, "NATIVE_RID_CONFLICT", "v2 digest"), Problem(status, code, "unknown")]

    def invoke(client, request):
        raise answers.pop(0)

    monkeypatch.setattr(NativeTurnGateway, "_invoke", staticmethod(invoke))
    client = SimpleNamespace(target=SimpleNamespace(agent_id=AGENT))
    with pytest.raises(Problem) as raised:
        submit_native_turn(SimpleNamespace(runtime=None), client,
                           InstructionSubmit(request_id="native-rid", content="Original text"), capture())
    assert (raised.value.status, raised.value.body["error"]["code"]) == (status, code) and answers == []


def test_a_resend_with_different_material_still_conflicts_and_is_tried_once(native_api, monkeypatch):  # noqa: F811
    case = native_api
    _, digests = _crashed_admission(case, monkeypatch)
    assert submit(case).status_code == 503
    from test_native_turn_api import PREFIX
    body = InstructionSubmit(request_id="native-rid", content="Other user text").model_dump(mode="json")
    changed = case.client.post(PREFIX + "/submit", json=body, headers=case.headers)
    assert changed.status_code == 409 and len(digests) == 3  # the v2 resend and one v1 resend, both refused
