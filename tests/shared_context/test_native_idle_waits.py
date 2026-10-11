"""Idle-only control while the original two Hosted lanes await a real typed question."""

from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace

import httpx
import pytest

from conftest import A, TENANT
from shared_context import native_sdk
from shared_context.behavior_native_turn_wire import NativeRead, NativeSessionFollow, NativeSessionRead, NativeSessionFilesOmission
from shared_context.errors import Problem
from shared_context.native_environment import follow_session
from shared_context.native_hitl_wire import NativeHitlTurn
from shared_context.native_turns import NativeTurnRuntime
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import hitl, post
from test_native_hitl_h2 import MODEL_ROOT, api_client
from test_native_hitl_questions import _lane_sdk, answer, page, read_question
from test_native_responses import response_submit, responses
from test_native_session_bootstrap import prepare
from test_native_turn_wire import submit
from test_native_turns import IDENTITY, OfflineSdk, local_env


@pytest.mark.parametrize("lane", ["foundry-native-v1", "foundry-responses-v1"])
def test_idle_follow_cannot_close_the_original_question_or_turn(hitl, responses, tmp_path, monkeypatch, lane):
    hitl.now = datetime.now(timezone.utc)
    original_scope = hitl.config.agent_scope
    hitl.config.targets += (SimpleNamespace(agent_id=IDENTITY.agent_id),)
    hitl.config.agent_scope = lambda identifier: (
        SimpleNamespace(tenant_id=TENANT, team_id=A.team, identity=A) if identifier == IDENTITY.agent_id
        else original_scope(identifier))
    asked = []
    _lane_sdk(monkeypatch, asked)

    def no_model(_):
        pytest.fail("this question and follow control must not start a model")

    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=Path(__file__), api_url="https://api.offline.invalid",
        model_url=MODEL_ROOT, token_provider=lambda: "OFFLINE", model_transport=httpx.MockTransport(no_model),
        hitl_client=api_client(hitl))
    executor.reader = responses.runtime
    responses.runtime.executor, responses.runtime.preflight = executor, executor.preflight
    assert prepare(responses).status_code == 200
    command = response_submit() if lane == "foundry-responses-v1" else submit()
    assert responses.invoke(command).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    if lane == "foundry-responses-v1":
        assert responses.respond(accepted).status_code == 200
    turn = NativeHitlTurn(agent_id=IDENTITY.agent_id, conversation_id=accepted.conversation_id, request_id="rid-1")
    deadline = time.monotonic() + 8
    while not (listed := page(hitl, turn))["attention"]:
        assert time.monotonic() < deadline, "the original question must reach the owner"
        time.sleep(0.01)
    item = listed["attention"][0]
    question = read_question(hitl, item["item_id"]).json()
    original = responses.invoke(NativeRead(request_id="rid-1")).json()
    state = responses.invoke(NativeSessionRead()).json()
    destination = IDENTITY.model_copy(update={"session_id": "idle-follow-destination", "agent_version": "2"})
    probe = NativeTurnRuntime(destination, OfflineSdk())
    pending = responses.call(follow_session, probe, NativeSessionFollow(
        request_id="idle-follow-question", expected_session_id=IDENTITY.session_id, expected_etag=state["etag"],
        stage="prepare", idle_only=True))
    assert pending.reason == "accepted_or_started" and not probe.executor.calls
    assert responses.invoke(NativeRead(request_id="rid-1")).json() == original
    assert read_question(hitl, item["item_id"]).json() == question
    with pytest.raises(Problem) as omission:
        responses.call(probe.dispatch, NativeSessionFilesOmission(
            request_id="no-files-cannot-close-question",
            follow=NativeSessionFollow(
                request_id="idle-follow-question", expected_session_id=IDENTITY.session_id,
                expected_etag=state["etag"], stage="prepare", idle_only=True, files_required=True),
            missing="destination"))
    assert omission.value.body["error"]["code"] == "NATIVE_FILES_CONFIRMATION_REQUIRED"
    assert responses.invoke(NativeRead(request_id="rid-1")).json() == original
    assert read_question(hitl, item["item_id"]).json() == question
    reply = answer({"question_id": item["item_id"], "question_digest": item["question"]["question_digest"]})
    assert post(hitl, "/answers", reply, read_only=True).status_code == 200
    completed = responses.wait("rid-1", "completed")
    assert completed.conversation_id == accepted.conversation_id and len(asked) == 1
    assert asked[0]["reply"] == {"answer": "blue", "wasFreeform": False}
