"""The continuation consumer requires slice 3's fresh ready/manifest facts, not metadata."""

import asyncio
from types import SimpleNamespace

import pytest

from shared_context.native_continuations import files_ready
from test_native_turns import harness, local_env, native_retry_policy
from test_native_responses import responses


@pytest.mark.parametrize("change", [
    None, "missing-manifest", "omitted", "pending", "wrong-agent", "wrong-sid", "wrong-version",
])
def test_files_readiness_uses_the_current_binding_and_exact_published_manifest_shape(change):
    # Exact read-side fields frozen by slice 3 at 2217f952; this is not a Files transport/live test.
    environment = SimpleNamespace(
        state="ready", files_manifest_sha256="a" * 64, agent_id="agent",
        hosted_session_id="target", hosted_agent_version="2")
    binding = SimpleNamespace(agent_id="agent", hosted_session_id="target", environment=environment)
    reads = []

    async def read_binding():
        reads.append(True)
        return binding, "original-etag"

    runtime = SimpleNamespace(
        identity=SimpleNamespace(agent_id="agent", session_id="target", agent_version="2"),
        _agent_record=read_binding)
    if change == "missing-manifest":
        del environment.files_manifest_sha256
    elif change == "omitted":
        environment.files_migrated = False
        environment.files_skip_request_id = "original-owner-omission"
        environment.files_manifest_sha256 = None
    elif change == "pending":
        environment.state = "pending"
    elif change == "wrong-agent":
        binding.agent_id = "another"
    elif change == "wrong-sid":
        binding.hosted_session_id = "source"
    elif change == "wrong-version":
        environment.hosted_agent_version = "1"
    assert asyncio.run(files_ready(runtime)) is (change is None)
    environment.state = "pending"
    assert asyncio.run(files_ready(runtime)) is False
    assert len(reads) == 2


def test_a_queued_input_cannot_start_sdk_in_another_execution_version(harness):
    from azure.ai.agentserver.core.tasks import TaskContext
    from shared_context.errors import Problem
    from shared_context.native_turns import NativeTaskInput
    from test_native_turns import IDENTITY, accept_without_chain

    original = accept_without_chain(harness)
    supplied = NativeTaskInput(
        request_id=original.value.receipt.request_id, accepted_etag=original.etag,
        call_id=original.value.call_id, execution_version="2")
    ctx = TaskContext(task_id=IDENTITY.agent_id, session_id=IDENTITY.session_id,
                      input_id=supplied.request_id, input=supplied)
    with pytest.raises(Problem) as mismatch:
        harness.call(harness.runtime.handle_turn, ctx)
    assert mismatch.value.body["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert harness.call(harness.runtime.read, supplied.request_id) == original.value.receipt
    assert harness.sdk.calls == []


def test_invocations_expected_version_refusal_preserves_the_original_accepted_record(harness):
    from shared_context.behavior_native_turn_wire import NATIVE_EXPECTED_VERSION_HEADER, NATIVE_TURN_HEADER, NativeRead
    from test_native_turns import IDENTITY, accept_without_chain

    original = accept_without_chain(harness).value.receipt
    result = harness.client.post("/invocations", json=NativeRead(request_id=original.request_id).model_dump(mode="json"),
        headers={NATIVE_TURN_HEADER: "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
                 NATIVE_EXPECTED_VERSION_HEADER: "2"})
    assert result.status_code == 409 and result.json()["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert harness.call(harness.runtime.read, original.request_id) == original and harness.sdk.calls == []


def test_responses_expected_version_refusal_never_starts_the_original_accepted_input(responses):
    from shared_context.behavior_native_turn_wire import NATIVE_EXPECTED_VERSION_HEADER
    from test_native_responses import response_submit
    from test_native_turns import IDENTITY

    request = response_submit("expected-version-original")
    assert responses.invoke(request).status_code == 202
    original = responses.call(responses.runtime.read, request.command.request_id)
    response = responses.client.post("/responses", json={
        "input": original.command.content, "background": True, "store": True, "stream": False,
        "agent_session_id": IDENTITY.session_id,
    }, headers={
        "x-agent-session-id": IDENTITY.session_id, NATIVE_EXPECTED_VERSION_HEADER: "2",
        "x-client-fa-rid": original.request_id, "x-client-fa-cid": original.conversation_id,
        "x-client-fa-submit-digest": original.submit_digest,
    })
    assert response.status_code == 200
    final = responses.response_result(response.json()["id"])
    assert final["status"] == "failed" and final["error"]["code"] == "NATIVE_SESSION_CHANGED"
    assert responses.call(responses.runtime.read, original.request_id) == original
    assert responses.sdk.calls == []


def test_new_sid_ready_control_resumes_pending_input_once_without_moving_the_old_task(local_env, monkeypatch):
    import copy
    from starlette.testclient import TestClient
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from shared_context.behavior_hosted import create_host
    from shared_context.behavior_native_turn_wire import (
        NativeContinuationRead, NativeContinuationSubmit, NativeSessionRead, continuation_ready_id,
    )
    from shared_context.errors import Problem
    from shared_context.native_continuation_wire import NativeContinuationReady
    from shared_context.native_effects import NativeEffects
    from shared_context.native_turns import NativeTurnRuntime
    from test_native_environment import invoke
    from test_native_session_bootstrap import prepare
    from test_native_turn_wire import submit
    from test_native_turns import Harness, IDENTITY, OfflineSdk

    first_sdk = OfflineSdk()
    first_runtime = NativeTurnRuntime(IDENTITY, first_sdk)

    async def unavailable(_view):
        raise Problem(409, "NATIVE_SESSION_CHANGED", "OFFLINE destination is not ready yet")

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first_runtime, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    first_runtime.executor, first_runtime.on_continuation = tracked, unavailable
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first_runtime)) as client:
        source_host = Harness(first_runtime, first_sdk, client)
        prepare(source_host)
        source_host.invoke(submit("cross-sid-base"))
        base = source_host.wait("cross-sid-base", "completed")
        first_sdk.block = True
        source_host.invoke(submit("cross-sid-root", base.conversation_id))
        source = source_host.wait("cross-sid-root", "started")
    old_sdk = OfflineSdk()
    old_runtime = NativeTurnRuntime(IDENTITY, old_sdk)
    old_runtime.on_continuation = unavailable
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=old_runtime)) as client:
        recovered = Harness(old_runtime, old_sdk, client)
        unknown = recovered.wait(source.request_id, "unknown")
        view = recovered.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        from shared_context.native_continuations import pending_handoffs
        async def retained():
            for _ in range(100):
                if source.request_id in await pending_handoffs(old_runtime):
                    return
                await asyncio.sleep(0.01)
            pytest.fail("the official recovery did not retain its handoff reference")
        recovered.call(retained)
        state = recovered.invoke(NativeSessionRead()).json()
        assert old_sdk.calls == []

    identity = IDENTITY.model_copy(update={"session_id": "continuation-destination", "agent_version": "2"})
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", identity.session_id)
    sdk = OfflineSdk()

    async def inactive(_sid):
        return True

    async def quiet(sid, **_options):
        assert sid == IDENTITY.session_id
        return True

    runtime = NativeTurnRuntime(identity, sdk, bootstrap_source_inactive=inactive,
                                task_observer=SimpleNamespace(quiet=quiet))
    submitted = []

    async def dispatch(ready):
        submitted.append(ready.request_id)
        original = await runtime.read(ready.original_request_id)
        return await runtime.continue_turn(NativeContinuationSubmit(
            command=original.command.model_copy(update={
                "request_id": ready.request_id, "conversation_id": original.conversation_id}),
            capture=original.capture, continuation=ready.continuation))

    runtime.on_continuation = dispatch
    with TestClient(create_host(SimpleNamespace(identity=identity), native_runtime=runtime)) as client:
        target = Harness(runtime, sdk, client)
        follow = {"operation": "session_follow", "request_id": "original-file-handoff",
                  "expected_session_id": IDENTITY.session_id, "expected_etag": state["etag"], "stage": "prepare"}
        assert invoke(client, identity, follow).json()["reason"] == "source_stop_required"
        ready = invoke(client, identity, {**follow, "stage": "commit"})
        assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
        signal = NativeContinuationReady(
            request_id=continuation_ready_id(follow["request_id"], identity.session_id, "a" * 64),
            environment_request_id=follow["request_id"], hosted_session_id=identity.session_id,
            files_manifest_sha256="a" * 64)
        assert invoke(client, identity, signal).status_code == 409
        assert sdk.calls == [] and submitted == []
        waiting = invoke(client, identity, NativeContinuationRead(request_id=source.request_id)).json()
        assert waiting["state"] == "prepared"
        assert waiting["failure_code"] == "NATIVE_CONTINUATION_WORKSPACE_REQUIRED"
        assert target.call(runtime.read, source.request_id) == unknown

        async def publish_files_fact():
            # Slice 3's verified publication/CAS is the explicit I/O substitute here; no file copying is claimed.
            async with FoundryStateStore("fa/v1/turns") as store:
                item = await store.get_item("agent")
                value = copy.deepcopy(item.value)
                value["environment"]["files_manifest_sha256"] = "a" * 64
                await store.set_item("agent", value, if_match=item.etag)

        target.call(publish_files_fact)
        sent = invoke(client, identity, signal)
        assert sent.status_code == 200, sent.text
        completed = target.wait(view["request_id"], "completed")
        assert completed.hosted_session_id == identity.session_id
        assert completed.conversation_id == source.conversation_id and completed.continuation.attempt == 1
        assert sdk.calls[0][2].sha256 == first_sdk.calls[-1][2].sha256
        assert target.call(runtime.read, source.request_id) == unknown
        assert invoke(client, identity, signal).status_code == 200
        assert submitted == [completed.request_id] and len(sdk.calls) == 1
