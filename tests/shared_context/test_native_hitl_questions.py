"""B1 PA-11: SDK ask_user questions on the H1 control store, signed local HTTP and the H2 Hosted wait."""

import asyncio
from datetime import timedelta
import json

import pytest

from conftest import A
from shared_context.errors import Problem
from shared_context.native_hitl import pending_id, question_id, validate_native_hitl_history
from shared_context.native_hitl_wire import (
    NativeHitlAnswer, NativeHitlCall, NativeHitlEnd, NativeHitlPendingQuery, NativeHitlQuestion,
    NativeHitlQuestionCreate, NativeHitlQuestionReadback, NativeHitlTurn,
)
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_hitl import create_pending, decision, error, hitl, inventory, post  # noqa: F401
from test_native_hitl_h2 import SID, setup_gate
from test_native_responses import responses  # noqa: F401
from test_native_turns import local_env  # noqa: F401

PREFIX = "/native-hitl/v1"


def ask(c, *, call_id="ask-user-local-1", request_id="question-one", choices=("red", "blue"), freeform=True,
        rid="rid-one", sdk=SID):
    question = NativeHitlQuestion(
        call=NativeHitlCall(agent_id="native-a", conversation_id="cid-one", request_id=rid, sdk_session_id=sdk,
                           tool_call_id=call_id),
        question="Which colour should the synthetic chart use?", choices=choices, allow_freeform=freeform)
    return NativeHitlQuestionCreate(request_id=request_id, question=question, question_digest=question.sha256)


def create_question(c, **kwargs):
    command = ask(c, **kwargs)
    response = post(c, "/questions", command, c.agent)
    assert response.status_code == 200, response.text
    return command, response.json()["result"]


def answer(created, text="blue", *, freeform=False, request_id="answer-one"):
    return NativeHitlAnswer(request_id=request_id, question_id=created["question_id"],
                            question_digest=created["question_digest"], answer=text, was_freeform=freeform)


def read_question(c, qid, principal=None):
    return c.client.get(PREFIX + "/questions/" + qid, headers=c.headers(principal))


def page(c, turn, principal=None):
    response = c.client.post(PREFIX + "/turn-questions", json=NativeHitlPendingQuery(turn=turn).model_dump(mode="json"),
                             headers=c.headers(principal))
    assert response.status_code == 200, response.text
    return response.json()


TURN = NativeHitlTurn(agent_id="native-a", conversation_id="cid-one", request_id="rid-one")


def test_a_question_is_one_owner_reply_in_its_own_namespace_never_an_approval(hitl):
    c = hitl
    command, created = create_question(c)
    assert created["question_id"] == question_id(c.owner.tenant_id, A.team, command.question.call)
    assert created["question_id"] != pending_id(c.owner.tenant_id, A.team, command.question.call)
    listed = page(c, TURN)
    assert [item["type"] for item in listed["attention"]] == ["question_required"]
    item = listed["attention"][0]
    assert (item["text"], item["choices"], item["allow_freeform"]) == (
        command.question.question, ["red", "blue"], True)
    assert item["question"] == listed["references"][0] and item["purpose"] == "attention-only-not-approval"
    # The approval reader keeps its own scope: no question appears as a pending approval.
    approvals = c.client.post(PREFIX + "/turn-pending", json=NativeHitlPendingQuery(turn=TURN).model_dump(mode="json"),
                              headers=c.headers()).json()
    assert approvals["references"] == approvals["attention"] == []
    before = len(c.table.submissions)
    # An ordinary owner reply, without Behavior.Approve; the Agent itself cannot answer.
    error(post(c, "/answers", answer(created), c.agent), 403)
    replied = post(c, "/answers", answer(created), read_only=True)
    assert replied.status_code == 200, replied.text
    assert replied.json()["result"]["status"] == "answered"
    assert post(c, "/answers", answer(created), read_only=True).json() == replied.json()
    error(post(c, "/answers", answer(created, "red", request_id="answer-two")), 409, "NATIVE_HITL_STATE_CONFLICT")
    view = NativeHitlQuestionReadback.model_validate_json(read_question(c, created["question_id"], c.agent).content)
    assert view.status == "answered" and view.answer == answer(created) and view.attention is None
    assert page(c, TURN)["attention"] == []
    assert c.service.rules(c.owner).revision == 0
    kinds = {type(value).__name__ for value in validate_native_hitl_history(inventory(c), c.config).values()}
    assert kinds == {"_Intent", "NativeHitlQuestionPending", "_QuestionAnswer", "_TurnIndex"}
    assert len(c.table.submissions) > before


def test_questions_and_approvals_never_take_each_others_writers(hitl):
    c = hitl
    _, created = create_question(c)
    _, pending = create_pending(c)
    as_decision = decision(c, {"pending_id": created["question_id"], "pending_digest": created["question_digest"]})
    error(post(c, "/decisions", as_decision), 409, "NATIVE_HITL_RECORD_INVALID")
    error(c.client.get(PREFIX + "/pending/" + created["question_id"], headers=c.headers()), 409,
          "NATIVE_HITL_RECORD_INVALID")
    as_answer = answer({"question_id": pending["pending_id"], "question_digest": pending["pending_digest"]})
    error(post(c, "/answers", as_answer), 409, "NATIVE_HITL_RECORD_INVALID")
    error(read_question(c, pending["pending_id"]), 409, "NATIVE_HITL_RECORD_INVALID")
    error(post(c, "/answers", answer(created, "green")), 422, "NATIVE_HITL_ANSWER_INVALID")
    wrong = answer(created).model_copy(update={"question_digest": "f" * 64})
    error(post(c, "/answers", wrong), 409, "NATIVE_HITL_QUESTION_CHANGED")
    _, strict = create_question(c, call_id="ask-user-local-2", request_id="question-two", freeform=False)
    error(post(c, "/answers", answer(strict, "my own words", freeform=True, request_id="free")), 422,
          "NATIVE_HITL_ANSWER_INVALID")
    assert post(c, "/answers", answer(strict, "blue", request_id="choice")).status_code == 200
    error(post(c, "/questions", ask(c, call_id="ask-user-local-2", request_id="question-again"), c.agent), 409,
          "NATIVE_HITL_CALL_EXISTS")
    error(post(c, "/questions", ask(c), c.other_agent), 403)


def test_expiry_and_a_closed_turn_refuse_answers_but_keep_an_existing_answer(hitl):
    c = hitl
    _, answered = create_question(c)
    _, expired = create_question(c, call_id="ask-user-local-2", request_id="question-two")
    assert post(c, "/answers", answer(answered)).status_code == 200
    c.now += timedelta(seconds=601)
    error(post(c, "/answers", answer(expired, request_id="late")), 410, "NATIVE_HITL_EXPIRED")
    assert page(c, TURN)["attention"] == []
    c.now -= timedelta(seconds=601)
    assert post(c, "/end", NativeHitlEnd(request_id="end-one", turn=TURN), c.agent).status_code == 200
    error(post(c, "/answers", answer(expired, request_id="closed")), 409, "NATIVE_HITL_TURN_CLOSED")
    error(post(c, "/questions", ask(c, call_id="ask-user-local-3", request_id="question-three"), c.agent), 409,
          "NATIVE_HITL_TURN_CLOSED")
    kept = NativeHitlQuestionReadback.model_validate_json(read_question(c, answered["question_id"]).content)
    assert kept.status == "answered" and kept.turn_closed and kept.answer.answer == "blue"
    closed = NativeHitlQuestionReadback.model_validate_json(read_question(c, expired["question_id"]).content)
    assert closed.status == "pending" and closed.turn_closed and closed.attention is None


def test_lost_question_ack_only_reconciles_the_original(hitl):
    c = hitl
    command, created = create_question(c)
    before = len(c.table.submissions)
    again = post(c, "/questions/reconcile", command, c.agent)
    assert again.status_code == 200 and again.json()["result"] == created
    assert post(c, "/questions", command, c.agent).json()["result"] == created
    assert len(c.table.submissions) == before


async def _until(predicate):
    for _ in range(400):
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.005)
    pytest.fail("the original question never reached its expected state")


@pytest.mark.parametrize("freeform", [False, True])
def test_the_hosted_wait_returns_only_the_owners_answer_to_the_sdk(hitl, tmp_path, freeform):
    case = setup_gate(hitl, tmp_path)

    async def check():
        waiting = asyncio.create_task(case.gate.user_input(
            {"question": "Which colour?", "choices": ["red", "blue"], "allowFreeform": True}, {"session_id": SID}))
        listed = await _until(lambda: _questions(case))
        item = listed["attention"][0]
        assert not waiting.done()
        assert item["question"]["call"]["tool_call_id"].startswith("ask-user-")
        assert item["question"]["call"]["sdk_session_id"] == SID and item["question"]["call"]["request_id"] == "rid-one"
        reply = answer({"question_id": item["item_id"], "question_digest": item["question"]["question_digest"]},
                       "Use teal" if freeform else "blue", freeform=freeform)
        assert post(hitl, "/answers", reply).status_code == 200
        assert await waiting == {"answer": reply.answer, "wasFreeform": freeform}
        # A second question in the same turn is a new local call and never sees the first answer.
        second = asyncio.create_task(case.gate.user_input({"question": "Which colour?", "choices": ["red", "blue"]},
                                                          {"session_id": SID}))
        listed = await _until(lambda: _questions(case))
        assert listed["attention"][0]["item_id"] != item["item_id"]
        assert not second.done()
        await case.gate.close()
        with pytest.raises(Problem) as closed:
            await second
        assert closed.value.body["error"]["code"] == "NATIVE_HITL_TURN_CLOSED"
        assert len(case.sender.observed) == 0
    asyncio.run(check())


async def _questions(case):
    value = page(case.c, case.gate.turn)
    return value if value["attention"] else None


@pytest.mark.parametrize("failure", [(412, "BEHAVIOR_CONFLICT"), (503, "NATIVE_HITL_AUTHORITY_UNAVAILABLE")])
def test_a_failed_read_during_the_wait_is_read_again_so_a_saved_answer_is_never_dropped(hitl, tmp_path, failure):
    case = setup_gate(hitl, tmp_path)
    original, failed = case.client.read_question, []

    async def flaky(qid):
        if failed == [] and (await original(qid)).status == "pending" and getattr(case, "answered", False):
            failed.append(failure)
            raise Problem(*failure, "OFFLINE read raced the owner's answer")
        return await original(qid)

    case.client.read_question = flaky

    async def check():
        waiting = asyncio.create_task(case.gate.user_input({"question": "Which colour?", "choices": ["red", "blue"]},
                                                           {"session_id": SID}))
        item = (await _until(lambda: _questions(case)))["attention"][0]
        case.answered = True
        await _until(lambda: asyncio.sleep(0, failed))
        reply = answer({"question_id": item["item_id"], "question_digest": item["question"]["question_digest"]})
        assert post(hitl, "/answers", reply).status_code == 200
        assert await waiting == {"answer": "blue", "wasFreeform": False}
        assert failed == [failure]
        # A read that keeps failing still ends at the original deadline, never as an invented answer. A human turn
        # waits for the user (Neal 10-10), so that deadline is the 24 h interactive bound, not 600 s.
        later = asyncio.create_task(case.gate.user_input({"question": "Again?"}, {"session_id": SID}))
        await _until(lambda: _open_besides(case, item["item_id"]))

        async def down(_qid):
            raise Problem(*failure, "OFFLINE read unavailable")
        case.client.read_question = down
        hitl.now += timedelta(days=1, seconds=1)
        with pytest.raises(Problem) as expired:
            await later
        assert expired.value.body["error"]["code"] == "NATIVE_HITL_EXPIRED"
        hitl.now -= timedelta(days=1, seconds=1)
        await case.gate.close()
    asyncio.run(check())


def test_expiry_wrong_session_and_a_new_runtime_never_invent_or_reuse_an_answer(hitl, tmp_path):
    case = setup_gate(hitl, tmp_path)

    async def check():
        with pytest.raises(Problem) as scope:
            await case.gate.user_input({"question": "Continue?"}, {"session_id": "another-sdk"})
        assert scope.value.body["error"]["code"] == "NATIVE_HITL_SCOPE_DENIED"
        waiting = asyncio.create_task(case.gate.user_input({"question": "Continue?"}, {"session_id": SID}))
        first = (await _until(lambda: _questions(case)))["attention"][0]
        hitl.now += timedelta(days=1, seconds=1)
        with pytest.raises(Problem) as expired:
            await waiting
        assert expired.value.body["error"]["code"] == "NATIVE_HITL_EXPIRED"
        hitl.now -= timedelta(days=1, seconds=1)
        restarted = setup_gate(hitl, tmp_path / "restart").gate
        again = asyncio.create_task(restarted.user_input({"question": "Continue?"}, {"session_id": SID}))
        listed = await _until(lambda: _open_besides(case, first["item_id"]))
        assert listed["question"]["call"]["tool_call_id"] != first["question"]["call"]["tool_call_id"]
        assert not again.done()
        again.cancel()
        with pytest.raises(asyncio.CancelledError):
            await again
        await restarted.close()
    asyncio.run(check())


async def _open_besides(case, identifier):
    value = page(case.c, case.gate.turn)
    return next((item for item in value["attention"] if item["item_id"] != identifier), None)


def test_wire_bounds_keep_one_question_one_inline_row():
    call = NativeHitlCall(agent_id="a", conversation_id="c", request_id="r", sdk_session_id="s", tool_call_id="t")
    NativeHitlQuestion(call=call, question="x" * 2000, choices=tuple("y" * 200 for _ in range(10)))
    for bad in ({"question": ""}, {"question": "x" * 2001}, {"choices": ("y",) * 11}, {"choices": ("",)}):
        with pytest.raises(ValueError):
            NativeHitlQuestion(**{"call": call, "question": "ok", **bad})
    question = NativeHitlQuestion(call=call, question="ok")
    with pytest.raises(ValueError):
        NativeHitlQuestionCreate(request_id="q", question=question, question_digest="0" * 64)
    assert json.loads(question.model_dump_json())["choices"] == []


def _lane_sdk(monkeypatch, asked):
    """The two Hosted handlers with a synthetic SDK session; no runtime, model or tool process."""
    import subprocess
    from threading import Event
    from types import SimpleNamespace
    from unittest.mock import create_autospec
    from copilot import CopilotClient
    from copilot.client import GetStatusResponse
    from copilot.session import CopilotSession
    from copilot.session_events import AssistantMessageData, SessionIdleData
    from shared_context import m2_copilot

    def denied(*args, **kwargs):
        pytest.fail("no SDK executable or real subprocess is permitted")

    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)

    def factory(**connection):
        client = create_autospec(CopilotClient, instance=True)
        exited = Event()
        client._cli_process = SimpleNamespace(pid=999, poll=lambda: 0 if exited.is_set() else None, returncode=0)
        client.stop.side_effect = exited.set
        client.get_status.return_value = GetStatusResponse(version="1.0.83", protocol_version=2)

        async def create(**options):
            sid = options["session_id"]
            session = create_autospec(CopilotSession, instance=True)
            session.session_id = sid
            session.get_events.return_value = []
            handlers = []
            session.on.side_effect = lambda handler: handlers.append(handler) or (lambda: handlers.remove(handler))

            async def send(prompt, **kwargs):
                record = {"available": list(options["available_tools"]), "excluded": list(options["excluded_tools"]),
                          "handler": options.get("on_user_input_request")}
                asked.append(record)
                text = "OFFLINE no question"
                if record["handler"] is not None:
                    reply = await record["handler"](
                        {"question": "Which colour?", "choices": ["red", "blue"], "allowFreeform": True},
                        {"session_id": sid})
                    record["reply"] = reply
                    text = "OFFLINE used " + reply["answer"]
                fs = options["create_session_fs_handler"](SimpleNamespace(session_id=sid))
                await fs.write_file("/session-state/events.jsonl", json.dumps(
                    {"type": "session.start", "data": {"sessionId": sid}}) + "\n")
                await fs.write_file("/session-state/workspace.yaml", "id: " + sid + "\n")
                for handler in handlers:
                    handler(SimpleNamespace(data=AssistantMessageData(content=text, message_id="offline-answer")))
                    handler(SimpleNamespace(data=SessionIdleData()))
                return "offline-original-message"

            session.send.side_effect = send
            return session

        client.create_session.side_effect = create
        return client

    monkeypatch.setattr(m2_copilot, "CopilotClient", create_autospec(CopilotClient, side_effect=factory))


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("lane", ["foundry-native-v1", "foundry-responses-v1"])
def test_each_lane_offers_ask_user_only_with_hitl_and_returns_the_owners_answer(
        hitl, responses, tmp_path, monkeypatch, lane, enabled):
    import time
    from datetime import datetime, timezone
    from pathlib import Path as FilePath
    from types import SimpleNamespace
    import httpx
    from conftest import TENANT
    from shared_context import native_sdk
    from shared_context.behavior_native_turn_wire import NativeResult, NativeSubmit
    from test_native_hitl_h2 import MODEL_ROOT, api_client
    from test_native_responses import response_submit
    from test_native_turn_wire import submit
    from test_native_turns import IDENTITY

    hitl.now = datetime.now(timezone.utc)
    original_scope = hitl.config.agent_scope
    hitl.config.targets += (SimpleNamespace(agent_id=IDENTITY.agent_id),)
    hitl.config.agent_scope = lambda identifier: (
        SimpleNamespace(tenant_id=TENANT, team_id=A.team, identity=A) if identifier == IDENTITY.agent_id
        else original_scope(identifier))
    asked = []
    _lane_sdk(monkeypatch, asked)

    def model(request):
        pytest.fail("a question needs no independent review or model call")

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=FilePath(__file__), api_url="https://api.offline.invalid",
        model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE", model_transport=httpx.MockTransport(model),
        hitl_client=api_client(hitl) if enabled else None)
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    command = response_submit() if lane == "foundry-responses-v1" else submit()
    assert responses.invoke(NativeSubmit.model_validate_json(command.model_dump_json())).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    if lane == "foundry-responses-v1":
        assert responses.respond(accepted).status_code == 200
    if enabled:
        turn = NativeHitlTurn(agent_id=IDENTITY.agent_id, conversation_id=accepted.conversation_id, request_id="rid-1")
        deadline = time.monotonic() + 8
        while not (listed := page(hitl, turn))["attention"]:
            assert time.monotonic() < deadline, "the original lane did not persist its question"
            time.sleep(0.01)
        item = listed["attention"][0]
        assert responses.call(responses.runtime.read, "rid-1").status == "started"
        reply = answer({"question_id": item["item_id"], "question_digest": item["question"]["question_digest"]})
        assert post(hitl, "/answers", reply, read_only=True).status_code == 200
    completed = responses.wait("rid-1", "completed")
    result = responses.invoke(NativeResult(conversation_id=completed.conversation_id, request_id="rid-1"))
    assert len(asked) == 1
    if enabled:
        assert "builtin:ask_user" in asked[0]["available"] and "builtin:*" not in asked[0]["excluded"]
        assert asked[0]["reply"] == {"answer": "blue", "wasFreeform": False}
        assert result.json()["text"] == "OFFLINE used blue"
        final = NativeHitlQuestionReadback.model_validate_json(read_question(hitl, item["item_id"]).content)
        assert final.status == "answered" and final.turn_closed
    else:
        assert asked[0]["handler"] is None
        assert "builtin:ask_user" not in asked[0]["available"] and "builtin:*" in asked[0]["excluded"]
        assert result.json()["text"] == "OFFLINE no question"
