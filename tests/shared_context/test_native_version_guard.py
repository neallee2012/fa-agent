"""FA's expected-version guard is not a Foundry routing header."""

import pytest

from shared_context.behavior_native_turn_wire import NATIVE_STATE_PROFILE, NATIVE_TURN_HEADER
from shared_context.errors import Problem
from test_native_responses import responses, response_submit
from test_native_turns import IDENTITY, harness, local_env, native_retry_policy
from test_native_turn_wire import submit


@pytest.mark.parametrize("expected", ["@latest", "01", "", "not-a-version"])
def test_malformed_expected_version_is_rejected_without_an_operation(harness, expected):
    response = harness.client.post("/invocations", json=submit("invalid-version").model_dump(mode="json"), headers={
        NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "offline-invalid-version", "x-agent-foundry-call-id": "offline-version-caller",
        "x-client-fa-expected-version": expected})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NATIVE_REQUEST_INVALID"
    assert not harness.sdk.calls


def test_wrong_actual_version_never_admits_or_starts_a_native_turn(harness):
    response = harness.client.post("/invocations", json=submit("wrong-version").model_dump(mode="json"), headers={
        NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE,
        "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "offline-version-check",
        "x-agent-foundry-call-id": "offline-version-caller",
        "x-client-fa-expected-version": str(int(IDENTITY.agent_version) + 1),
    })
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert not harness.sdk.calls
    with pytest.raises(Problem) as absent:
        harness.call(harness.runtime.read, "wrong-version")
    assert absent.value.body["error"]["code"] == "NATIVE_RID_MISSING"


def test_wrong_actual_version_cannot_dispatch_the_accepted_responses_turn(responses):
    command = response_submit()
    assert responses.invoke(command).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    result = responses.client.post("/responses", json={
        "input": accepted.command.content, "background": True, "store": True, "stream": False,
        "agent_session_id": IDENTITY.session_id,
    }, headers={
        "x-agent-session-id": IDENTITY.session_id, "x-agent-foundry-call-id": "offline-version-caller",
        "x-client-fa-rid": accepted.request_id, "x-client-fa-cid": accepted.conversation_id,
        "x-client-fa-submit-digest": accepted.submit_digest,
        "x-client-fa-expected-version": str(int(IDENTITY.agent_version) + 1),
    })
    assert result.status_code == 200, result.text
    terminal = responses.response_result(result.json()["id"])
    assert terminal["status"] == "failed"
    assert terminal["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert not responses.sdk.calls
    assert responses.call(responses.runtime.read, "rid-1") == accepted


def _read_invocation(harness, command):
    return harness.client.post("/invocations", json=command.model_dump(mode="json"), headers={
        NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "offline-replaced", "x-agent-foundry-call-id": "offline-replaced-caller",
        "x-client-fa-expected-version": IDENTITY.agent_version})


def test_a_session_replaced_by_a_newer_environment_refuses_record_reads_but_not_its_session_read(harness):
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativeRead, NativeSessionRead
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    assert harness.invoke(submit("before-replacement")).status_code == 202
    harness.wait("before-replacement", "completed")
    assert _read_invocation(harness, NativeRead(request_id="before-replacement")).status_code == 200

    async def follow_elsewhere():
        # Another process commits the follow; this process's memory is not told.
        current, etag = await harness.runtime._agent_record()
        newer = NativeEnvironmentView(
            agent_id=IDENTITY.agent_id, request_id="newer-follow", state="ready",
            previous_hosted_session_id=IDENTITY.session_id, previous_agent_version=IDENTITY.agent_version,
            hosted_session_id="OFFLINE-newer-session", hosted_agent_version=str(int(IDENTITY.agent_version) + 1))
        replaced = _changed(current, hosted_session_id=newer.hosted_session_id, environment=newer, chain_task_id=None)
        async with harness.runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(replaced), if_match=etag)

    harness.call(follow_elsewhere)
    refused = _read_invocation(harness, NativeRead(request_id="before-replacement"))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    # The session read stays available: the API compares it to find the Agent's current environment.
    state = _read_invocation(harness, NativeSessionRead())
    assert state.status_code == 200 and state.json()["hosted_session_id"] == "OFFLINE-newer-session"
    assert len(harness.sdk.calls) == 1
