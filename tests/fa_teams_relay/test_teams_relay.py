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
        self.replies, self.posts, self.paths = list(replies), [], []

    def __call__(self, request):
        self.paths.append(request.url.path)
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

        def say(content="今天的待辦？", *, activity="act-1", user=OWNER):
            response = client.post("/responses", json={
                "input": content, "stream": False, "conversation": "conv_teams_a",
                "metadata": {"ActivityId": activity, "ActivityProtocolConversationId": "a:teams-chat"},
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
    assert case.say() == "這一輪還在跑，完成後結果在工作台。"
    assert case.clock.now - 1000.0 <= 110 and sum(case.clock.sleeps) == 108
    assert len(case.fake.posts) == 37


@pytest.mark.parametrize("failure", [(503, {"error": {"code": "TABLE_UNAVAILABLE"}}), None])
def test_an_api_that_never_answered_2xx_says_receipt_is_unconfirmed_at_110_seconds(chat, failure):
    case = chat(failure)
    assert case.say() == "暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。"
    assert sum(case.clock.sleeps) == 108 and len(case.fake.posts) == 37


def test_api_errors_after_a_running_reply_still_say_running_at_110_seconds(chat):
    case = chat(running(), (503, {"error": {"code": "TABLE_UNAVAILABLE"}}))
    assert case.say() == "這一輪還在跑，完成後結果在工作台。"
    assert sum(case.clock.sleeps) == 108 and len(case.fake.posts) == 37


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
    assert case.say("允許 123456") == "這一輪還在跑，完成後結果在工作台。"
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
