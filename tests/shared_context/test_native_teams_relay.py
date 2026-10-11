"""Teams relay route (N2 T1): signed relay identity -> owner check -> the owner's Responses turn, Hosted/platform fake."""

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from conftest import InMemoryRepository
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeReceipt, NativeResultReference, NativeSubmit
from shared_context.errors import Problem
from shared_context.native_hitl_attention import approval_code
from shared_context.native_hitl_wire import NativeHitlDecision
from shared_context.native_teams_relay import (
    AGENTS_ENV, DECISIONS_PATH, ENABLED_ENV, PATH, install_teams_relay_routes,
)
from test_m3_auth import token
from test_native_responses_api import responses_api  # noqa: F401
from test_native_turn_api import native_api  # noqa: F401
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401

RELAY = "0e0e0e0e-0000-4000-8000-000000000001"
STRANGER = "0e0e0e0e-0000-4000-8000-000000000002"


class FakeHitl:
    """The HITL service answers this route uses; the service's own decision rules are covered by test_native_hitl*.py."""

    def __init__(self, agent_id, waits, seen):
        self.agent_id, self.waits, self.seen = agent_id, waits, seen
        self.cards, self.submitted, self.collide = {}, [], False

    def card(self, pid, request_id, *, conversation_id="conv-server-1", category="shell", status="pending",
             expired=False):
        call = SimpleNamespace(agent_id=self.agent_id, conversation_id=conversation_id, request_id=request_id)
        self.cards[pid] = SimpleNamespace(
            pid=pid, call=call, category=category, status=status, expired=expired, turn_closed=False, decision=None)

    def pending(self, principal, query):
        self.seen.append(("approval", principal.oid, principal.kind, query.turn.request_id))
        return SimpleNamespace(attention=tuple(
            SimpleNamespace(approval=SimpleNamespace(pending_id=card.pid)) for card in self.cards.values()
            if card.status == "pending" and not card.expired and card.call.request_id == query.turn.request_id))

    def questions(self, principal, query):
        self.seen.append(("question", principal.oid, principal.kind, query.turn.request_id))
        return SimpleNamespace(attention=tuple(wait for wait in self.waits if wait == "question"))

    def consents(self, principal, query):
        self.seen.append(("consent", principal.oid, principal.kind, query.turn.request_id))
        return SimpleNamespace(attention=tuple(wait for wait in self.waits if wait == "consent"))

    def read(self, principal, pid):
        card = self.cards[pid]
        return SimpleNamespace(
            approval=SimpleNamespace(pending_id=pid, pending_digest="d" * 64, call=card.call),
            pending=SimpleNamespace(command=SimpleNamespace(action=SimpleNamespace(
                category=card.category, tool="shell"))),
            status=card.status, expired=card.expired, turn_closed=card.turn_closed,
            decision=None if card.decision is None else SimpleNamespace(choice=card.decision))

    def rules(self, principal):
        return SimpleNamespace(revision=0, etag=None)

    def submit(self, principal, command):
        self.submitted.append((principal, command))
        card = self.cards[command.pending_id]
        if card.status != "pending":
            raise Problem(409, "NATIVE_HITL_STATE_CONFLICT", "synthetic: already decided")
        card.status, card.decision = "allow" if command.choice == "allow_once" else "deny", command.choice


@pytest.fixture
def relay(responses_api, signing_key, monkeypatch):
    env = responses_api
    records, waits, seen = InMemoryRepository(), [], []
    hitl = FakeHitl(env.case.remote.target.agent_id, waits, seen)
    env.case.client.app.state.native_hitl = hitl
    monkeypatch.setattr("shared_context.native_teams_relay.find_approval_ids", lambda service, principal, code: [
        pid for pid in service.cards if service.collide or approval_code(pid) == code])
    environ = {ENABLED_ENV: "1", AGENTS_ENV: json.dumps({RELAY: env.case.remote.target.agent_id})}
    install_teams_relay_routes(env.case.client.app, env.case.composition, environ=environ, records=records)
    owner = env.case.composition.config.identity.human_oid

    def sign(body, path, oid, delegated):
        signed = (token(signing_key, {"oid": oid, "scp": "Behavior.Read"}) if delegated else
                  token(signing_key, {"oid": oid, "idtyp": "app"}, remove=("scp", "appid")))
        headers = {"Authorization": "Bearer " + signed}
        return env.case.client.post(path, json=body, headers=headers)

    def post(activity="act-1", text="今天的待辦？", *, chat="conv_teams_a", user=owner, oid=RELAY, delegated=False):
        body = {"activity_id": activity, "chat_id": chat, "text": text, **({} if user is None else {"user_oid": user})}
        return sign(body, PATH, oid, delegated)

    def decide(code="301355", choice="allow_once", *, activity="act-2", chat="conv_teams_a", user=owner, oid=RELAY,
               delegated=False, **extra):
        body = {"activity_id": activity, "chat_id": chat, "choice": choice, "code": code, **extra,
                **({} if user is None else {"user_oid": user})}
        return sign(body, DECISIONS_PATH, oid, delegated)

    return SimpleNamespace(env=env, records=records, post=post, decide=decide, owner=owner, waits=waits, seen=seen,
                           hitl=hitl)


def submits(env):
    return [call for call in env.calls if call.url.path.endswith("/invocations")
            and json.loads(call.content)["operation"] == "submit"]


def complete(env, rid, text):
    original = env.case.remote.records[rid]
    env.case.remote.single_text = text
    raw = text.encode("utf-8")
    env.case.remote.records[rid] = NativeReceipt.model_validate({
        **original.model_dump(), "status": "completed", "turn_number": 1, "response_id": "resp-original",
        "dispatch": None, "result": NativeResultReference(
            turn_number=1, turn_key="turn:1", result_digest="a" * 64, text_sha256=hashlib.sha256(raw).hexdigest(),
            text_bytes=len(raw), text_parts=1, checkpoint_sha256="b" * 64)})


def rid(activity, chat="conv_teams_a"):
    return "tr-" + hashlib.sha256(f"{RELAY}|{chat}|{activity}".encode()).hexdigest()[:40]


def test_owner_message_runs_one_turn_and_returns_its_reply(relay):
    first = relay.post()
    assert first.status_code == 200, first.text
    assert first.json() == {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}
    original = relay.env.case.remote.records[rid("act-1")]
    assert original.command.content == "今天的待辦？" and original.source is None
    complete(relay.env, rid("act-1"), "三件事。")
    done = relay.post()
    assert done.json() == {"state": "done", "text": "三件事。"}
    assert len(submits(relay.env)) == 1 and len(relay.env.conversations) == 1


def test_same_activity_resent_reads_the_original_turn_and_never_submits_again(relay):
    for _ in range(3):
        assert relay.post().json()["state"] == "running"
    assert len(submits(relay.env)) == 1
    assert list(relay.env.case.remote.records) == [rid("act-1")]


def test_the_same_activity_id_in_another_chat_is_its_own_turn(relay):
    for chat in ("conv_teams_a", "conv_teams_b", "conv_teams_a"):
        assert relay.post("act-1", chat=chat).json()["state"] == "running"
    assert sorted(relay.env.case.remote.records) == sorted([rid("act-1"), rid("act-1", "conv_teams_b")])
    assert len(submits(relay.env)) == 2


def racing(env, monkeypatch, missing):
    """This request read ``missing`` before a concurrent first send was admitted (one miss); then Hosted's own
    create-or-read refuses its different body with 409, as ``_submit_locked`` does (the shared fake would not)."""
    inner = env.case.foundry.http._transport.handler

    def hosted(request):
        nonlocal missing
        if request.url.path.endswith("/invocations"):
            command = NATIVE_REQUEST.validate_json(request.content)
            if command.operation == "read" and command.request_id == missing:
                missing = None
                env.calls.append(request)
                return httpx.Response(404, json={"error": {"code": "NATIVE_RID_MISSING"}})
            if isinstance(command, NativeSubmit):
                existing = env.case.remote.records.get(command.command.request_id)
                if existing is not None and existing.submit_digest != command.sha256:
                    env.calls.append(request)
                    return httpx.Response(409, json={"error": {"code": "NATIVE_RID_CONFLICT", "message": "private"}})
        return inner(request)

    monkeypatch.setattr(env.case.foundry.http, "_transport", httpx.MockTransport(hosted))


def test_a_concurrent_first_send_reads_the_original_turn_instead_of_failing(relay, monkeypatch):
    assert relay.post().json()["state"] == "running"
    racing(relay.env, monkeypatch, rid("act-1"))
    late = relay.post()
    assert late.status_code == 200, late.text
    assert late.json() == {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}
    assert len(submits(relay.env)) == 2 and list(relay.env.case.remote.records) == [rid("act-1")]
    assert relay.env.case.remote.records[rid("act-1")].command.conversation_id is None
    complete(relay.env, rid("act-1"), "好。")
    racing(relay.env, monkeypatch, rid("act-1"))
    assert relay.post().json() == {"state": "done", "text": "好。"}


def test_same_activity_with_other_text_is_a_conflict_not_a_second_turn(relay):
    assert relay.post().status_code == 200
    conflict = relay.post(text="別的內容")
    assert conflict.status_code == 409, conflict.text
    assert len(submits(relay.env)) == 1


@pytest.mark.parametrize("user", [STRANGER, None])
def test_anyone_but_the_owner_gets_the_fixed_text_and_no_turn_or_record(relay, user):
    reply = relay.post(user=user)
    assert reply.json() == {"state": "refused", "text": "這個助理只回應它的主人。"}
    assert relay.env.calls == [] and relay.env.case.remote.records == {} and relay.records.data == {}


@pytest.mark.parametrize("caller", [{"oid": STRANGER}, {"delegated": True}])
def test_identity_outside_the_relay_allowlist_is_403_without_a_turn(relay, caller):
    reply = relay.post(**caller)
    assert reply.status_code == 403, reply.text
    assert relay.env.calls == [] and relay.records.data == {}


def test_teams_chat_is_created_once_and_the_next_message_continues_that_conversation(relay):
    assert relay.post("act-1").status_code == 200
    first = relay.env.case.remote.records[rid("act-1")]
    complete(relay.env, rid("act-1"), "好。")
    assert relay.post("act-1").json()["state"] == "done"
    assert relay.post("act-2", "下一步？").status_code == 200
    second = relay.env.case.remote.records[rid("act-2")]
    assert first.conversation_id == "conv-server-1"
    assert second.command.conversation_id == "conv-server-1" and len(relay.env.conversations) == 1
    assert [value.payload["conversation_id"] for value in relay.records.data.values()] == ["conv-server-1"]
    other_chat = relay.post("act-3", "新對話", chat="conv_teams_b")
    assert other_chat.status_code == 200
    assert relay.env.case.remote.records[rid("act-3", "conv_teams_b")].command.conversation_id is None
    assert len(relay.records.data) == 2 and len(relay.env.conversations) == 2


def test_a_lost_teams_chat_write_is_repaired_from_the_original_receipt(relay, monkeypatch):
    create = relay.records.create

    def lost(*args):
        raise Problem(503, "TABLE_UNAVAILABLE", "synthetic lost write")

    monkeypatch.setattr(relay.records, "create", lost)
    assert relay.post().status_code == 503
    monkeypatch.setattr(relay.records, "create", create)
    assert relay.post().json()["state"] == "running"
    assert [value.payload["conversation_id"] for value in relay.records.data.values()] == ["conv-server-1"]
    assert len(submits(relay.env)) == 1


def test_a_refused_queue_answers_busy_and_is_not_resent(relay):
    relay.env.state["response_failure"] = 409
    assert relay.post("act-0", "先做這個").json()["state"] == "running"
    relay.env.state.update(response_failure=None, holder=rid("act-0"))
    busy = relay.post("act-1")
    assert busy.json() == {"state": "busy", "text": "Agent 正在忙，請稍後再傳。"}
    assert relay.post("act-1").json()["state"] == "busy"
    assert relay.env.case.remote.records[rid("act-1")].command.conversation_id == "conv-server-1"
    assert len(submits(relay.env)) == 2 and len(relay.records.data) == 1


WORKBENCH = "這一步需要你批准，請到工作台處理，批准後結果會在工作台。"
NO_CARD = "這個代碼沒有對應的待批准項目（可能已處理或過期）。"


@pytest.mark.parametrize("kind,text", [
    ("question", "助理有問題要問你，請到工作台回答，結果會在工作台。"),
    ("consent", "需要連線，請到工作台處理，結果會在工作台。")])
def test_a_question_or_connect_card_still_points_to_the_workbench(relay, kind, text):
    relay.waits.append(kind)
    reply = relay.post()
    assert reply.json() == {"state": "needs_approval", "text": text}
    assert (kind, relay.owner, "human", rid("act-1")) in relay.seen
    relay.waits.clear()
    assert relay.post().json()["state"] == "running"


def test_an_approval_card_gives_one_command_pair_per_open_card(relay):
    assert relay.post().json()["state"] == "running"
    relay.hitl.card("a" * 64, rid("act-1"))
    relay.hitl.card("b" * 64, rid("act-1"))
    assert relay.post().json() == {"state": "needs_approval", "text": (
        "這一步需要你批准（shell）。允許請回覆「允許 301355」，拒絕請回覆「拒絕 301355」；也可以到工作台處理。\n"
        "這一步需要你批准（shell）。允許請回覆「允許 066836」，拒絕請回覆「拒絕 066836」；也可以到工作台處理。")}
    assert ("approval", relay.owner, "human", rid("act-1")) in relay.seen


def test_a_routine_card_stays_workbench_only(relay):
    assert relay.post().json()["state"] == "running"
    relay.hitl.card("a" * 64, rid("act-1"), category="routine")
    assert relay.post().json() == {"state": "needs_approval", "text": WORKBENCH}
    assert relay.decide("301355").json() == {"state": "failed", "text": WORKBENCH}
    assert relay.hitl.submitted == []


@pytest.fixture
def waiting(relay):
    """One Teams message whose turn is running and waits on card a (code 301355)."""
    assert relay.post().json()["state"] == "running"
    relay.hitl.card("a" * 64, rid("act-1"))
    return relay


def test_allow_approves_the_one_matching_card_and_returns_the_original_turns_reply(waiting):
    relay = waiting
    first = relay.decide("301355")
    assert first.status_code == 200, first.text
    assert first.json() == {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}
    [(principal, command)] = relay.hitl.submitted
    assert isinstance(command, NativeHitlDecision) and command.choice == "allow_once" and command.rule is None
    assert command.pending_id == "a" * 64 and command.pending_digest == "d" * 64
    assert (principal.oid, principal.kind, principal.actor_id) == (relay.owner, "human", None)
    assert "approve" in principal.actions
    complete(relay.env, rid("act-1"), "三件事。")
    assert relay.decide("301355").json() == {"state": "done", "text": "三件事。"}
    assert len(relay.hitl.submitted) == 1 and len(submits(relay.env)) == 1


def test_deny_records_the_choice_and_says_so_while_the_turn_still_runs(waiting):
    relay = waiting
    assert relay.decide("301355", "deny").json() == {"state": "done", "text": "已拒絕，這一步沒有執行。"}
    assert relay.hitl.submitted[0][1].choice == "deny" and relay.hitl.cards["a" * 64].status == "deny"
    complete(relay.env, rid("act-1"), "好，我沒有做。")
    assert relay.decide("301355", "deny").json() == {"state": "done", "text": "好，我沒有做。"}
    assert len(relay.hitl.submitted) == 1


def test_the_same_decision_twice_is_the_same_success_not_a_conflict(waiting):
    relay = waiting
    answers = [relay.decide("301355") for _ in range(3)]
    assert [answer.status_code for answer in answers] == [200, 200, 200]
    assert [answer.json() for answer in answers] == [{"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}] * 3
    assert len(relay.hitl.submitted) == 1


@pytest.mark.parametrize("error", [
    Problem(412, "BEHAVIOR_CONFLICT", "synthetic: lost the write"),
    Problem(409, "NATIVE_HITL_ALREADY_AUTHORIZED", "synthetic: already crossed its authorization point")])
@pytest.mark.parametrize("winner,reply", [
    ("allow_once", {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}),
    ("deny", {"state": "failed", "text": "這個項目已處理過。"})])
def test_a_lost_write_race_answers_from_the_saved_state(waiting, monkeypatch, error, winner, reply):
    relay, card = waiting, waiting.hitl.cards["a" * 64]

    def other_decision_first(principal, command):
        card.status, card.decision = "allow" if winner == "allow_once" else "deny", winner
        raise error

    monkeypatch.setattr(relay.hitl, "submit", other_decision_first)
    assert relay.decide("301355", "allow_once").json() == reply


def test_a_lost_commit_right_after_a_rules_change_asks_for_the_message_again(waiting, monkeypatch):
    relay = waiting
    rules = iter([SimpleNamespace(revision=0, etag=None), SimpleNamespace(revision=1, etag='W/"r1"')])
    sent = []
    errors = [Problem(409, "NATIVE_HITL_RULES_CHANGED", "synthetic: rules changed"),
              Problem(412, "BEHAVIOR_CONFLICT", "synthetic: lost the write")]

    def rules_change_then_lost_commit(principal, command):
        sent.append((command.request_id, command.expected_rules_revision))
        raise errors.pop(0)

    monkeypatch.setattr(relay.hitl, "rules", lambda principal: next(rules))
    monkeypatch.setattr(relay.hitl, "submit", rules_change_then_lost_commit)
    assert relay.decide("301355").json() == {"state": "failed", "text": "這次沒有送成，請再傳一次同樣的指令。"}
    assert [revision for _, revision in sent] == [0, 1] and sent[0][0] != sent[1][0]
    assert errors == [] and relay.hitl.cards["a" * 64].status == "pending"


@pytest.mark.parametrize("closed", ["expired", "interrupted", "effect_unknown"])
def test_a_decided_card_that_hosted_later_closed_keeps_answering_its_saved_choice(waiting, closed):
    relay, card = waiting, waiting.hitl.cards["a" * 64]
    assert relay.decide("301355").status_code == 200
    card.status = closed
    assert relay.decide("301355").json() == {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}
    assert relay.decide("301355", "deny").json() == {"state": "failed", "text": "這個項目已處理過。"}
    assert len(relay.hitl.submitted) == 1


@pytest.mark.parametrize("closed,text", [
    ("interrupted", "這個項目已過期。"),
    ("effect_unknown", "這個項目已處理過。")])
def test_an_undecided_closed_card_is_expired_unless_a_rule_already_ran_it(waiting, closed, text):
    waiting.hitl.cards["a" * 64].status = closed
    assert waiting.decide("301355").json() == {"state": "failed", "text": text}
    assert waiting.hitl.submitted == []


def test_a_workbench_allow_by_rule_answers_a_teams_allow_with_the_original_turn(waiting):
    card = waiting.hitl.cards["a" * 64]
    card.status, card.decision = "allow", "allow_rule"
    assert waiting.decide("301355").json() == {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}
    assert waiting.decide("301355", "deny").json() == {"state": "failed", "text": "這個項目已處理過。"}
    assert waiting.hitl.submitted == []


@pytest.mark.parametrize("first,second", [("allow_once", "deny"), ("deny", "allow_once")])
def test_the_opposite_choice_on_a_decided_card_is_already_handled(waiting, first, second):
    relay = waiting
    assert relay.decide("301355", first).status_code == 200
    assert relay.decide("301355", second).json() == {"state": "failed", "text": "這個項目已處理過。"}
    assert len(relay.hitl.submitted) == 1


def test_an_expired_card_cannot_be_decided(waiting):
    card = waiting.hitl.cards["a" * 64]
    card.expired = True
    assert waiting.decide("301355").json() == {"state": "failed", "text": "這個項目已過期。"}
    card.expired, card.status = False, "expired"
    assert waiting.decide("301355").json() == {"state": "failed", "text": "這個項目已過期。"}
    assert waiting.hitl.submitted == []


def test_a_decision_that_loses_the_race_reads_the_saved_state_instead_of_failing(waiting, monkeypatch):
    relay = waiting
    submit = relay.hitl.submit

    def other_choice_first(principal, command):
        relay.hitl.cards["a" * 64].status = "deny"
        return submit(principal, command)

    monkeypatch.setattr(relay.hitl, "submit", other_choice_first)
    assert relay.decide("301355").json() == {"state": "failed", "text": "這個項目已處理過。"}


def test_a_wrong_code_finds_nothing_and_decides_nothing(waiting):
    assert waiting.decide("000000").json() == {"state": "failed", "text": NO_CARD}
    assert waiting.hitl.submitted == []


def test_a_card_of_another_conversation_is_not_found(waiting):
    waiting.hitl.card("c" * 64, rid("act-9"), conversation_id="conv-someone-else")
    assert waiting.decide("698122").json() == {"state": "failed", "text": NO_CARD}
    assert waiting.hitl.submitted == []


def test_a_repeated_code_asks_for_the_workbench(waiting):
    waiting.hitl.card("b" * 64, rid("act-1"))
    waiting.hitl.collide = True
    assert waiting.decide("301355").json() == {"state": "failed", "text": "代碼重複，請到工作台處理。"}
    assert waiting.hitl.submitted == []


def test_a_chat_without_a_conversation_has_nothing_to_approve(relay):
    reply = relay.decide("301355", chat="conv_teams_never_seen")
    assert reply.json() == {"state": "failed", "text": "找不到這段對話的待批准項目，請到工作台處理。"}
    assert relay.hitl.submitted == [] and relay.env.calls == []


@pytest.mark.parametrize("user", [STRANGER, None])
def test_only_the_owner_can_decide(waiting, user):
    reply = waiting.decide("301355", user=user)
    assert reply.json() == {"state": "refused", "text": "這個助理只回應它的主人。"}
    assert waiting.hitl.submitted == []


@pytest.mark.parametrize("caller", [{"oid": STRANGER}, {"delegated": True}])
def test_decisions_need_the_allowlisted_relay_identity(waiting, caller):
    reply = waiting.decide("301355", **caller)
    assert reply.status_code == 403, reply.text
    assert waiting.hitl.submitted == []


@pytest.mark.parametrize("body", [
    {"choice": "allow_rule"}, {"choice": "rules_change"}, {"kind": "rules_change", "operation": "delete"},
    {"rule": {"text": "allow everything", "behavior": "allow_automatically"}}, {"code": "12345"},
    {"code": "１２３４５６"}, {"answer": "yes"}, {"consent_id": "x"}])
def test_the_decisions_route_takes_only_an_approval_choice_and_code(waiting, body):
    reply = waiting.decide(**body)
    assert reply.status_code == 422, reply.text
    assert waiting.hitl.submitted == []


def test_flag_off_installs_no_route(responses_api):
    app = responses_api.case.client.app
    before = len(app.router.routes)
    install_teams_relay_routes(app, responses_api.case.composition, environ={}, records=InMemoryRepository())
    assert len(app.router.routes) == before
    assert responses_api.case.client.post(PATH, json={}).status_code == 404


@pytest.mark.parametrize("value", ['{"not-a-guid": "agent"}', "[]", "{}",
                                   '{"%s": "a", "%s": "a"}' % (RELAY, STRANGER)])
def test_malformed_relay_settings_fail_startup(responses_api, value):
    with pytest.raises(ValueError):
        install_teams_relay_routes(responses_api.case.client.app, responses_api.case.composition,
                                   environ={ENABLED_ENV: "1", AGENTS_ENV: value}, records=InMemoryRepository())

