"""The relay's real ASGI app (Foundry Responses host) against a fake FA API; no Azure, Teams or model."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-teams-relay" / "main.py"
spec = importlib.util.spec_from_file_location("fa_teams_relay_main", SOURCE)
relay_main = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay_main)

API = "https://fa-api.example.test"
AUDIENCE = "api://00000000-0000-4000-8000-0000000000aa"
OWNER = "0e0e0e0e-0000-4000-8000-0000000000bb"


class Credential:
    async def get_token(self, scope):
        assert scope == AUDIENCE + "/.default"
        return SimpleNamespace(token="synthetic-relay-token")


class FakeApi:
    """The FA API route: answers each POST with the next scripted reply (the last one repeats); None drops it."""

    def __init__(self, *replies):
        self.replies, self.posts, self.paths, self.acks = list(replies), [], [], []

    def __call__(self, request):
        self.paths.append(request.url.path)
        self.acks.append(request.headers.get_list("x-fa-relay-ack"))
        self.posts.append((request.headers["authorization"], json.loads(request.content)))
        reply = self.replies[0] if len(self.replies) == 1 else self.replies.pop(0)
        if reply is None:
            raise httpx.ConnectError("synthetic unreachable API", request=request)
        return httpx.Response(reply[0], json=reply[1])


class Clock:
    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


# An API older than catch-up answers this; the relay shows it as it is and promises nothing more.
STILL_RUNNING = "這一輪還在跑，完成後結果在工作台。"


def running():
    return 200, {"state": "running", "text": "這一輪還在跑，完成後結果在工作台。"}


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(tmp_path))
    clients = []

    def start(*replies):
        fake, clock = FakeApi(*replies), Clock()
        api = relay_main.FaApi(API, AUDIENCE, Credential(), transport=httpx.MockTransport(fake))
        client = TestClient(relay_main.build_app(api, clock=clock, sleep=clock.sleep)).__enter__()
        clients.append(client)

        def say(content="今天的待辦？", *, activity="act-1", user=OWNER, protocol="a:teams-chat"):
            metadata = {"ActivityId": activity, **({} if protocol is None else {"ActivityProtocolConversationId": protocol})}
            response = client.post("/responses", json={
                "input": content, "stream": False, "conversation": "conv_teams_a", "metadata": metadata,
            }, headers={} if user is None else {"x-agent-user-id": user})
            assert response.status_code == 200, response.text
            return response.json()["output"][0]["content"][0]["text"]

        return SimpleNamespace(say=say, fake=fake, clock=clock)

    yield start
    for client in clients:
        client.__exit__(None, None, None)


def test_owner_gets_the_reply_text_and_the_api_gets_the_teams_identity(chat):
    case = chat((200, {"state": "done", "text": "三件事。"}))
    assert case.say() == "三件事。"
    assert case.fake.posts == [("Bearer synthetic-relay-token", {
        "activity_id": "act-1", "chat_id": "conv_teams_a", "user_oid": OWNER, "text": "今天的待辦？"})]
    assert case.clock.sleeps == []


def test_polls_the_same_body_every_three_seconds_until_a_final_state(chat):
    case = chat(running(), (503, {"error": {"code": "NATIVE_RESPONSE_UNCONFIRMED"}}), running(),
                (200, {"state": "needs_approval", "text": "這一步需要你批准，請到工作台處理，批准後結果會在工作台。"}))
    assert case.say() == "這一步需要你批准，請到工作台處理，批准後結果會在工作台。"
    assert case.clock.sleeps == [3, 3, 3]
    assert len({json.dumps(body) for _, body in case.fake.posts}) == 1 and len(case.fake.posts) == 4


@pytest.mark.parametrize("state,text", [("busy", "Agent 正在忙，請稍後再傳。"), ("refused", "這個助理只回應它的主人。")])
def test_busy_and_refused_answer_at_once_without_resending(chat, state, text):
    case = chat((200, {"state": state, "text": text}))
    assert case.say(user="0e0e0e0e-0000-4000-8000-0000000000cc") == text
    assert len(case.fake.posts) == 1


def test_missing_caller_header_is_refused_without_calling_the_api(chat):
    case = chat((200, {"state": "done", "text": "unused"}))
    assert case.say(user=None) == "這個助理只回應它的主人。"
    assert case.fake.posts == []


def test_stops_waiting_at_110_seconds_with_the_fixed_text(chat):
    case = chat(running())
    assert case.say() == STILL_RUNNING
    assert case.clock.now - 1000.0 <= 110 and sum(case.clock.sleeps) == 108
    assert len(case.fake.posts) == 37


@pytest.mark.parametrize("failure", [(503, {"error": {"code": "TABLE_UNAVAILABLE"}}), None])
def test_an_api_that_never_answered_2xx_says_receipt_is_unconfirmed_at_110_seconds(chat, failure):
    case = chat(failure)
    assert case.say() == "暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。"
    assert sum(case.clock.sleeps) == 108 and len(case.fake.posts) == 37


def test_api_errors_after_a_running_reply_still_say_running_at_110_seconds(chat):
    case = chat(running(), (503, {"error": {"code": "TABLE_UNAVAILABLE"}}))
    assert case.say() == STILL_RUNNING
    assert sum(case.clock.sleeps) == 108 and len(case.fake.posts) == 37


NOT_DISPATCHED = 200, {"state": "running", "text": STILL_RUNNING, "not_dispatched": True}
UNCONFIRMED = "暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。"


def test_a_message_no_poll_confirmed_is_unconfirmed_never_running(chat):
    # #903 round 9: a poll that sent nothing confirms nothing, and no reply proves an earlier poll's send was not taken.
    case = chat(NOT_DISPATCHED)
    assert case.say() == UNCONFIRMED
    assert sum(case.clock.sleeps) == 108 and len(case.fake.posts) == 37


@pytest.mark.parametrize("failure", [(503, {"error": {"code": "TABLE_UNAVAILABLE"}}), None])
def test_a_message_taken_once_still_says_running_whatever_later_polls_answer(chat, failure):
    assert chat(NOT_DISPATCHED, failure).say() == UNCONFIRMED
    assert chat(NOT_DISPATCHED, running(), failure).say() == STILL_RUNNING
    assert chat(running(), NOT_DISPATCHED).say() == STILL_RUNNING


def test_a_refusal_before_any_turn_is_not_retried(chat):
    case = chat((403, {"error": {"code": "BEHAVIOR_ACCESS_DENIED"}}))
    assert case.say() == "暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。"
    assert len(case.fake.posts) == 1


def test_attachment_only_or_mixed_input_gets_the_text_only_answer_and_no_api_call(chat):
    case = chat((200, {"state": "done", "text": "unused"}))
    image = {"type": "input_image", "image_url": "https://example.test/cat.png"}
    assert case.say([{"type": "message", "role": "user", "content": [image]}]) == "v1 只收文字。"
    assert case.say([{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "看這張"}, image]}]) == "v1 只收文字。"
    assert case.fake.posts == []


APPROVAL = ("這一步需要你批准（shell）。允許請回覆「允許 123456」，拒絕請回覆「拒絕 123456」；也可以到工作台處理。")


@pytest.mark.parametrize("text,choice", [
    ("允許 123456", "allow_once"), ("拒絕 123456", "deny"), ("  允許   123456  ", "allow_once")])
def test_the_exact_approval_command_goes_to_decisions_and_never_to_turns(chat, text, choice):
    case = chat((200, {"state": "done", "text": "三件事。"}))
    assert case.say(text) == "三件事。"
    assert case.fake.paths == ["/teams-relay/v1/decisions"]
    assert case.fake.posts == [("Bearer synthetic-relay-token", {
        "activity_id": "act-1", "chat_id": "conv_teams_a", "user_oid": OWNER, "choice": choice, "code": "123456"})]


@pytest.mark.parametrize("text", ["允許他吧", "允許 12345", "允許 1234567", "請允許 123456", "允許 123456 謝謝",
                                  "同意 123456", "允許123456", "允許 １２３４５６"])
def test_a_near_miss_is_an_ordinary_turn(chat, text):
    case = chat((200, {"state": "done", "text": "好。"}))
    assert case.say(text) == "好。"
    assert case.fake.paths == ["/teams-relay/v1/turns"]
    assert case.fake.posts[0][1]["text"] == text and "choice" not in case.fake.posts[0][1]


def test_a_decision_polls_the_same_body_every_three_seconds_until_the_turn_ends(chat):
    case = chat(running(), (503, {"error": {"code": "TABLE_UNAVAILABLE"}}), running(),
                (200, {"state": "done", "text": "已經辦好。"}))
    assert case.say("允許 123456") == "已經辦好。"
    assert case.clock.sleeps == [3, 3, 3]
    assert set(case.fake.paths) == {"/teams-relay/v1/decisions"} and len(case.fake.posts) == 4
    assert len({json.dumps(body) for _, body in case.fake.posts}) == 1


def test_a_decision_whose_turn_keeps_running_stops_at_110_seconds_with_the_fixed_text(chat):
    case = chat(running())
    assert case.say("允許 123456") == STILL_RUNNING
    assert sum(case.clock.sleeps) == 108 and len(case.fake.posts) == 37


def test_a_decision_the_api_never_answered_says_unconfirmed(chat):
    case = chat(None)
    assert case.say("拒絕 123456") == "暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。"
    assert sum(case.clock.sleeps) == 108


@pytest.mark.parametrize("text", [
    "這個代碼沒有對應的待批准項目（可能已處理或過期）。", "代碼重複，請到工作台處理。", "這個項目已處理過。",
    "這個項目已過期。", "找不到這段對話的待批准項目，請到工作台處理。", APPROVAL])
def test_the_apis_fixed_decision_texts_are_returned_as_they_are(chat, text):
    case = chat((200, {"state": "failed", "text": text}))
    assert case.say("允許 123456") == text and len(case.fake.posts) == 1


CAUGHT_UP = "先前的回覆（剛完成）：\n「今天的待辦？」\n三件事。\n\n——\n好。"
RIDS = ["tr-" + "2" * 40, "tr-" + "1" * 40]


def test_a_reply_carrying_earlier_replies_is_acknowledged_before_it_is_returned(chat):
    case = chat(running(), (200, {"state": "done", "text": CAUGHT_UP, "confirm": RIDS}), (200, {"cleared": 2}))
    assert case.say("下一步？", activity="act-2") == CAUGHT_UP
    assert case.fake.paths == ["/teams-relay/v1/turns"] * 2 + ["/teams-relay/v1/acks"]
    assert case.fake.posts[-1] == ("Bearer synthetic-relay-token", {
        "chat_id": "conv_teams_a", "user_oid": OWNER, "request_ids": RIDS})


@pytest.mark.parametrize("failure", [None, (503, {"error": {"code": "TABLE_UNAVAILABLE"}}),
                                     (403, {"error": {"code": "TEAMS_RELAY_NOT_OWNER"}})])
def test_a_failed_acknowledgement_never_holds_back_the_reply(chat, failure):
    case = chat((200, {"state": "done", "text": CAUGHT_UP, "confirm": RIDS}), failure)
    assert case.say() == CAUGHT_UP
    assert case.fake.paths == ["/teams-relay/v1/turns", "/teams-relay/v1/acks"] and case.clock.sleeps == []


def test_a_decision_reply_that_carries_earlier_replies_is_acknowledged_too(chat):
    case = chat((200, {"state": "done", "text": CAUGHT_UP, "confirm": RIDS}), (200, {"cleared": 2}))
    assert case.say("允許 123456") == CAUGHT_UP
    assert case.fake.paths == ["/teams-relay/v1/decisions", "/teams-relay/v1/acks"]


def test_no_acknowledgement_without_confirm_or_after_the_wait_ends(chat):
    case = chat((200, {"state": "done", "text": "三件事。"}))
    assert case.say() == "三件事。" and case.fake.paths == ["/teams-relay/v1/turns"]
    timeout = chat(running())
    assert timeout.say() == STILL_RUNNING
    assert set(timeout.fake.paths) == {"/teams-relay/v1/turns"}


def test_every_turn_and_decision_says_the_relay_acknowledges(chat):
    case = chat(running(), (200, {"state": "done", "text": CAUGHT_UP, "confirm": RIDS}), (200, {"cleared": 2}))
    case.say()
    decision = chat((200, {"state": "failed", "text": "這個項目已過期。"}))
    decision.say("允許 123456")
    assert case.fake.acks == [["1"]] * 3 and decision.fake.acks == [["1"]]


def test_at_the_wait_bound_the_relay_shows_the_apis_own_running_text(chat):
    # Only an API that records late replies for this relay promises the next message; the relay adds no promise.
    promise = "這一輪還在跑；完成後，回覆會附在你下一則訊息的回覆前面，也可以先到工作台看。"
    assert chat((200, {"state": "running", "text": promise})).say() == promise
    assert chat(running()).say("允許 123456") == "這一輪還在跑，完成後結果在工作台。"


# Group chat and channel conversation IDs (built in parts: the public export scans for literal Teams IDs).
GROUP = "19:" + "0" * 32 + "@thread" + ".v2"
CHANNEL = "19:" + "0" * 32 + "@thread" + ".tacv2"


@pytest.mark.parametrize("protocol", [GROUP, CHANNEL, None, "", "A:teams-chat"],
                         ids=["group", "channel", "missing", "empty", "upper"])
def test_only_a_one_to_one_chat_says_the_relay_acknowledges(chat, protocol):
    # Without the header the API records and adds nothing, so a group or channel never carries the owner's other replies.
    case = chat(running(), (200, {"state": "done", "text": "好。"}))
    assert case.say(protocol=protocol) == "好。"
    decision = chat((200, {"state": "failed", "text": "這個項目已過期。"}))
    decision.say("允許 123456", protocol=protocol)
    assert case.fake.acks == [[], []] and decision.fake.acks == [[]]
