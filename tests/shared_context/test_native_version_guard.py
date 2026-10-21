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


def _read_invocation(harness, command, *, located=False):
    """``located``: the API located this session by its binding row (``x-client-fa-locator``)."""
    return harness.client.post("/invocations", json=command.model_dump(mode="json"), headers={
        NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "offline-replaced", "x-agent-foundry-call-id": "offline-replaced-caller",
        "x-client-fa-expected-version": IDENTITY.agent_version,
        **({"x-client-fa-locator": "binding-row"} if located else {})})


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
    # ADR-0009 §4: the refusal names itself as before dispatch, why, and the VM's own version; the code is unchanged.
    error = refused.json()["error"]
    assert (error["not_dispatched"], error["reason"], error["actual_version"]) == (True, "replaced", IDENTITY.agent_version)


def test_version_refusal_says_not_dispatched_why_and_the_vms_own_version(harness):
    response = harness.client.post("/invocations", json=submit("drifted").model_dump(mode="json"), headers={
        NATIVE_TURN_HEADER: NATIVE_STATE_PROFILE, "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "offline-drifted", "x-agent-foundry-call-id": "offline-version-caller",
        "x-client-fa-expected-version": str(int(IDENTITY.agent_version) + 1)})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "NATIVE_SESSION_CHANGED"
    assert (error["not_dispatched"], error["reason"], error["actual_version"]) == (True, "version", IDENTITY.agent_version)
    assert not harness.sdk.calls


def test_profile_refusal_says_not_dispatched_why_and_the_vms_own_version(harness):
    response = harness.client.post("/invocations", json=submit("other-profile").model_dump(mode="json"), headers={
        # a profile this runtime does not declare (it declares v2 and keeps the v1 alias, ADR-0009 2d-ii)
        NATIVE_TURN_HEADER: "foundry-native-v3", "x-agent-session-id": IDENTITY.session_id,
        "x-agent-invocation-id": "offline-profile", "x-agent-foundry-call-id": "offline-version-caller",
        "x-client-fa-expected-version": IDENTITY.agent_version})
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "NATIVE_TURNS_DISABLED"
    assert (error["not_dispatched"], error["reason"], error["actual_version"]) == (True, "profile", IDENTITY.agent_version)
    assert not harness.sdk.calls
    with pytest.raises(Problem) as absent:
        harness.call(harness.runtime.read, "other-profile")
    assert absent.value.body["error"]["code"] == "NATIVE_RID_MISSING"


def _move_agent_record(harness, **changes):
    from shared_context.native_turns import TURNS_STORE, _changed, _json

    async def move():
        current, etag = await harness.runtime._agent_record()
        async with harness.runtime._store(TURNS_STORE) as store:
            await store.set_item("agent", _json(_changed(current, chain_task_id=None, **changes)), if_match=etag)

    harness.call(move)


def test_the_pending_follow_target_serves_record_reads_even_on_a_lower_version(harness):
    """ADR-0009 §2: the record names the target SID only at commit, and drift makes versions non-monotonic (source
    created on 2, this target on 1). The target's reads are served; comparing versions refused them."""
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativeRead

    assert harness.invoke(submit("before-follow")).status_code == 202
    harness.wait("before-follow", "completed")
    pending = NativeEnvironmentView(
        agent_id=IDENTITY.agent_id, request_id="drifted-follow", state="pending", reason="scan_pending",
        previous_hosted_session_id="OFFLINE-source-session", previous_agent_version=str(int(IDENTITY.agent_version) + 1),
        hosted_session_id=IDENTITY.session_id, hosted_agent_version=IDENTITY.agent_version)
    _move_agent_record(harness, hosted_session_id="OFFLINE-source-session", environment=pending)
    read = _read_invocation(harness, NativeRead(request_id="before-follow"))
    assert read.status_code == 200, read.text
    assert read.json()["request_id"] == "before-follow"


@pytest.mark.parametrize("state", ["pending", "ready"])
def test_a_sid_neither_current_nor_the_pending_target_is_replaced_whatever_its_version(harness, state):
    """The stricter half of ADR-0009 §2, in phase 3: a row-located call (the API reads only through the binding row's
    current SID) is refused on any other SID (``replaced``), even on the same version (M5 fresh sessions share their
    version). A call that does not say it was row-located keeps phase 2a's rule (next test)."""
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativeRead

    assert harness.invoke(submit("before-other")).status_code == 202
    harness.wait("before-other", "completed")
    other = NativeEnvironmentView(
        agent_id=IDENTITY.agent_id, request_id="other-follow", state=state,
        reason="scan_pending" if state == "pending" else None,
        previous_hosted_session_id="OFFLINE-source-session", previous_agent_version=IDENTITY.agent_version,
        hosted_session_id="OFFLINE-target-session", hosted_agent_version=IDENTITY.agent_version)
    current = "OFFLINE-source-session" if state == "pending" else "OFFLINE-target-session"
    _move_agent_record(harness, hosted_session_id=current, environment=other)
    refused = _read_invocation(harness, NativeRead(request_id="before-other"), located=True)
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["not_dispatched"], error["reason"]) == ("NATIVE_SESSION_CHANGED", True, "replaced")
    if state == "ready":
        # Its session read still answers, so the API can repair a binding row that is behind this record.
        from shared_context.behavior_native_turn_wire import NativeSessionRead
        assert _read_invocation(harness, NativeSessionRead()).json()["hosted_session_id"] == current


@pytest.mark.parametrize("state", ["pending", "ready"])
def test_a_call_not_located_by_a_row_keeps_phase_2a_rule_for_a_sid_on_the_records_version(harness, state):
    """An Agent without a usable binding row is still read through the selector's candidate SID, which during a
    pending retarget or a rolling deploy can be neither the current SID nor the pending target. Without the locator
    header Hosted refuses it only when the record names a newer version, as in phase 2a, so that read is served."""
    from shared_context.behavior_native_turn_wire import NativeEnvironmentView, NativeRead

    assert harness.invoke(submit("before-unlocated")).status_code == 202
    harness.wait("before-unlocated", "completed")
    other = NativeEnvironmentView(
        agent_id=IDENTITY.agent_id, request_id="other-follow", state=state,
        reason="scan_pending" if state == "pending" else None,
        previous_hosted_session_id="OFFLINE-source-session", previous_agent_version=IDENTITY.agent_version,
        hosted_session_id="OFFLINE-target-session", hosted_agent_version=IDENTITY.agent_version)
    current = "OFFLINE-source-session" if state == "pending" else "OFFLINE-target-session"
    _move_agent_record(harness, hosted_session_id=current, environment=other)
    served = _read_invocation(harness, NativeRead(request_id="before-unlocated"))
    assert served.status_code == 200, served.text
    assert served.json()["request_id"] == "before-unlocated"


def test_session_read_names_the_vms_own_version(harness):
    from shared_context.behavior_native_turn_wire import NativeSessionRead, NativeSessionState

    state = _read_invocation(harness, NativeSessionRead())
    assert state.status_code == 200, state.text
    assert state.json()["actual_version"] == IDENTITY.agent_version
    # An older image's state (no field) still decodes, and the field is left out when absent.
    older = {key: value for key, value in state.json().items() if key != "actual_version"}
    decoded = NativeSessionState.model_validate(older)
    assert decoded.actual_version is None and "actual_version" not in decoded.model_dump(mode="json")


def test_a_host_without_a_native_runtime_refuses_before_dispatch_with_its_own_reason():
    import asyncio
    import json
    from types import SimpleNamespace

    from shared_context.native_turns import native_response

    request = SimpleNamespace(headers=SimpleNamespace(getlist=lambda name: []), state=SimpleNamespace())
    response = asyncio.run(native_response(request, None))
    error = json.loads(response.body)["error"]
    assert response.status_code == 403 and error["code"] == "NATIVE_TURNS_DISABLED"
    assert (error["not_dispatched"], error["reason"]) == (True, "runtime") and "actual_version" not in error
