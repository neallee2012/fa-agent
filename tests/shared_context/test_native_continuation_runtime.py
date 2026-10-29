"""Official process recovery hands off a new bounded input; the old RID stays unknown."""

import asyncio
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.native_turns import NativeTurnRuntime
from test_native_turn_wire import submit
from test_native_turns import Harness, IDENTITY, OfflineSdk, local_env


@pytest.mark.parametrize("handoff_fault", [None, "read", "azure", "classified"])
def test_official_recovery_restores_the_last_checkpoint_in_one_new_input_without_holding_the_old_slot(
    local_env, monkeypatch, tmp_path, handoff_fault,
):
    from shared_context.behavior_native_turn_wire import NativeContinuationRead, NativeContinuationSubmit
    from shared_context.native_append_wire import NativeAppend, NativeAppendRead
    from shared_context.native_effects import NativeEffects
    from shared_context.behavior_text_files import UserTextFile

    first_sdk = OfflineSdk()
    first_runtime = NativeTurnRuntime(IDENTITY, first_sdk)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first_runtime, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    async def unexpected_before_recovery(_view):
        raise AssertionError("No continuation is dispatched before official recovery")

    first_runtime.executor = tracked
    first_runtime.on_continuation = unexpected_before_recovery
    user_file = UserTextFile(name="report.txt", text="Original human attachment")
    updated_file = UserTextFile(name="report.txt", text="Updated human attachment")

    def attached(request, attachment=user_file):
        return request.model_copy(update={"command": request.command.model_copy(update={"files": (attachment,)})})

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first_runtime)) as client:
        first = Harness(first_runtime, first_sdk, client)
        first.invoke(attached(submit("completed-base")))
        base = first.wait("completed-base", "completed")
        first_sdk.block = True
        first.invoke(attached(
            submit("interrupted-root", base.conversation_id, "Continue the original work."), updated_file))
        source = first.wait("interrupted-root", "started")
        pending = first.invoke(NativeAppend(
            request_id=source.request_id, conversation_id=source.conversation_id,
            append_id="original-append", expected_submit_digest=source.submit_digest,
            content="Keep the original pending clarification."))
        assert pending.status_code == 202 and pending.json()["status"] == "accepted"

    second_sdk = OfflineSdk()
    from azure.ai.agentserver.core.tasks import RetryPolicy
    from shared_context.native_turns import NativePostTurnPending
    second_runtime = NativeTurnRuntime(IDENTITY, second_sdk, delivery_retry=RetryPolicy(
        max_attempts=3, initial_delay=0.01, max_delay=0.01, jitter=False, retry_on=(NativePostTurnPending,)))
    handoffs = []
    accepted, notified = asyncio.Event(), asyncio.Event()
    lost = []
    registered = []

    if handoff_fault == "read":
        from azure.ai.agentserver.core.storage import FoundryStateStore
        from azure.core.exceptions import ServiceResponseError
        read = FoundryStateStore.get_item

        async def unavailable(store, key, **options):
            if key == "continuation-handoffs" and not lost:
                lost.append(key)
                raise ServiceResponseError("OFFLINE handoff scheduling read unavailable")
            return await read(store, key, **options)

        monkeypatch.setattr(FoundryStateStore, "get_item", unavailable)
    elif handoff_fault in {"azure", "classified"}:
        from azure.ai.agentserver.core.tasks import Task
        from azure.ai.agentserver.core.tasks._client import TransportClassifiedError
        from azure.core.exceptions import AzureError
        from shared_context.native_continuations import pending_handoffs

        start = Task.start

        async def unavailable_start(work, **options):
            if (work.name == "fa-native-agent-enqueue-v1"
                    and getattr(options.get("input"), "continuation_of", None) == source.request_id and not lost):
                registered.append(source.request_id in await pending_handoffs(second_runtime))
                lost.append("enqueue")
                if handoff_fault == "classified":
                    raise TransportClassifiedError(
                        status=503, classification="transient", message="OFFLINE task API temporarily unavailable")
                raise AzureError("OFFLINE task start failed")
            return await start(work, **options)

        monkeypatch.setattr(Task, "start", unavailable_start)

    async def terminal(receipt):
        if receipt.request_id == source.request_id:
            notified.set()

    async def deliver(view):
        assert not second_runtime.execution_lock.locked()
        original = await second_runtime.read(view.continuation.continuation_of)
        handoffs.append(view.request_id)
        command = original.command.model_copy(update={
            "request_id": view.request_id, "conversation_id": original.conversation_id})
        result = await second_runtime.continue_turn(NativeContinuationSubmit(
            command=command, capture=original.capture, source=original.source,
            state_profile=original.state_profile, continuation=view.continuation))
        accepted.set()
        return result

    second_runtime.on_continuation = deliver
    second_runtime.on_terminal = terminal
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=second_runtime)) as client:
        second = Harness(second_runtime, second_sdk, client)
        unknown = second.wait(source.request_id, "unknown")
        second.call(asyncio.wait_for, notified.wait(), 2)
        second.call(asyncio.wait_for, accepted.wait(), 2)
        assert len(lost) == int(handoff_fault is not None)
        if handoff_fault in {"azure", "classified"}:
            assert registered == [True]
        state = second.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        assert state["continuation"]["root_request_id"] == source.request_id
        assert state["continuation"]["continuation_of"] == source.request_id
        assert state["continuation"]["attempt"] == 1
        derived = second.wait(state["request_id"], "completed")
        assert derived.conversation_id == source.conversation_id
        assert derived.request_id != source.request_id
        assert derived.continuation.attempt == 1
        assert derived.command.content == source.command.content
        assert second_sdk.calls[0][2] is not None
        assert second_sdk.calls[0][2].sha256 == first_sdk.calls[-1][2].sha256
        assert [entry[0] for entry in second_sdk.calls] == [derived.request_id]
        assert second.call(second_runtime.read, source.request_id) == unknown
        assert second.invoke(NativeContinuationRead(request_id=source.request_id)).json()["request_id"] == derived.request_id
        assert handoffs == [derived.request_id]
        from shared_context.native_continuations import read_continuation_context
        from shared_context.native_continuation_wire import NativeContinuationQuery
        import json

        page = second.call(read_continuation_context, second_runtime, derived,
                           NativeContinuationQuery(section="request"))
        assert json.loads(page.text)["command"]["content"] == source.command.content
        append_page = second.call(read_continuation_context, second_runtime, derived,
                                  NativeContinuationQuery(section="append"))
        append = json.loads(append_page.text)
        assert append["append_id"] == "original-append" and append["status"] == "unknown"
        assert append["request_id"] == source.request_id
        assert page.authorization is False and append_page.authorization is False
        assert second.invoke(NativeAppendRead(
            request_id=source.request_id, conversation_id=source.conversation_id,
            append_id="original-append")).json() == append
        from shared_context.behavior_native_turn_wire import NativeHistory
        from shared_context.native_sdk_profiles import native_context

        origin = native_context(derived, None).turn_origin
        assert origin.source_kind == "continuation" and origin.continuation == derived.continuation
        history = second.call(second_runtime.history, NativeHistory(conversation_id=derived.conversation_id))
        turn = next(item for item in history.turns if item.request_id == derived.request_id)
        assert turn.user_text is None and turn.agent_text is None
        assert turn.continuation == derived.continuation and turn.continuation_text == source.command.content

        from shared_context.native_sdk import NativeSdkExecutor
        from shared_context.native_turns import NativeTurnControl

        second.invoke(submit("later-human-turn", derived.conversation_id, "Read the original attachment."))
        later = second.wait("later-human-turn", "completed")
        reader = NativeSdkExecutor(
            root=tmp_path, runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.invalid",
            model_url="https://model.invalid", token_provider=lambda: "synthetic-token")
        reader.reader = second_runtime
        (tool,) = reader._tools(later, ("read_user_file",), NativeTurnControl())
        original_file = json.loads(second.call(tool.handler, {
            "tool_name": "read_user_file", "tool_call_id": "read-original-file",
            "arguments": {"name": user_file.name},
        }).text)
        assert original_file["text"] == updated_file.text and original_file["request_id"] == source.request_id
        assert original_file["turn_number"] == source.turn_number
        assert [call[0] for call in second_sdk.calls] == [derived.request_id, later.request_id]


@pytest.mark.parametrize("lose_enqueue_start", [False, True])
def test_official_responses_recovery_dispatches_one_new_response_in_the_original_cid(
    local_env, monkeypatch, tmp_path, lose_enqueue_start,
):
    import httpx
    from shared_context.behavior_native_turn_wire import NativeContinuationRead, NativeContinuationSubmit
    from shared_context.native_effects import NativeEffects
    from test_native_responses import ResponsesHarness, build_responses_host, response_submit

    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    root = tmp_path / "journal"
    root.mkdir()
    first_sdk = OfflineSdk()
    first_runtime = NativeTurnRuntime(IDENTITY, first_sdk)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first_runtime, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    async def unexpected(_view):
        raise AssertionError("No continuation is dispatched before official recovery")

    first_runtime.executor = tracked
    first_runtime.on_continuation = unexpected
    contexts = {}
    with TestClient(build_responses_host(first_runtime, root, contexts)) as client:
        first = ResponsesHarness(first_runtime, first_sdk, client, contexts)
        first.invoke(response_submit("base-response"))
        first.respond(first.call(first_runtime.read, "base-response"))
        base = first.wait("base-response", "completed")
        first_sdk.block = True
        first.invoke(response_submit("interrupted-response", base.conversation_id))
        source = first.call(first_runtime.read, "interrupted-response")
        first.respond(source)
        source = first.wait(source.request_id, "started")

    second_sdk = OfflineSdk()
    from azure.ai.agentserver.core.tasks import RetryPolicy, Task
    from azure.ai.agentserver.core.tasks._client import TransportClassifiedError
    from shared_context.native_turns import NativePostTurnPending
    second_runtime = NativeTurnRuntime(IDENTITY, second_sdk, delivery_retry=RetryPolicy(
        max_attempts=3, initial_delay=0.01, max_delay=0.01, jitter=False, retry_on=(NativePostTurnPending,)))
    contexts = {}
    app = build_responses_host(second_runtime, root, contexts)
    handoffs = []
    accepted, lost = asyncio.Event(), []
    notified = asyncio.Event()

    async def reported(receipt):
        if receipt.request_id == source.request_id:
            notified.set()

    second_runtime.on_terminal = reported
    if lose_enqueue_start:
        from shared_context.native_continuations import pending_handoffs
        start = Task.start

        async def unavailable(work, **options):
            if (work.name == "fa-native-agent-enqueue-v1"
                    and getattr(options.get("input"), "continuation_of", None) == source.request_id and not lost):
                assert source.request_id in await pending_handoffs(second_runtime)
                lost.append(True)
                raise TransportClassifiedError(
                    status=503, classification="transient", message="OFFLINE task API temporarily unavailable")
            return await start(work, **options)

        monkeypatch.setattr(Task, "start", unavailable)

    async def deliver(view):
        assert not second_runtime.execution_lock.locked()
        original = await second_runtime.read(view.original_request_id)
        result = await second_runtime.continue_turn(NativeContinuationSubmit(
            command=original.command.model_copy(update={
                "request_id": view.request_id, "conversation_id": original.conversation_id}),
            capture=original.capture, state_profile=original.state_profile, continuation=view.continuation))
        if result.status == "accepted" and result.dispatch.state == "needs_response":
            handoffs.append(result.request_id)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
                response = await http.post("/responses", json={
                    "input": result.command.content, "background": True, "store": True, "stream": False,
                    "agent_session_id": IDENTITY.session_id,
                }, headers={
                    "x-agent-session-id": IDENTITY.session_id, "x-client-fa-rid": result.request_id,
                    "x-client-fa-cid": result.conversation_id, "x-client-fa-submit-digest": result.submit_digest,
                })
                assert response.status_code == 200
        result = await second_runtime.read(view.request_id)
        accepted.set()
        return result

    second_runtime.on_continuation = deliver
    with TestClient(app) as client:
        second = ResponsesHarness(second_runtime, second_sdk, client, contexts)
        unknown = second.wait(source.request_id, "unknown")
        second.call(asyncio.wait_for, notified.wait(), 2)
        second.call(asyncio.wait_for, accepted.wait(), 2)
        assert len(lost) == int(lose_enqueue_start)
        view = second.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        derived = second.wait(view["request_id"], "completed")
        assert derived.continuation.continuation_of == source.request_id
        assert derived.continuation.attempt == 1 and derived.response_id != source.response_id
        assert derived.conversation_id == source.conversation_id
        assert contexts[source.request_id][0][0].is_recovery
        assert not contexts[derived.request_id][0][0].is_recovery
        assert second_sdk.calls[0][2].sha256 == first_sdk.calls[-1][2].sha256
        assert [call[0] for call in second_sdk.calls] == handoffs == [derived.request_id]
        assert second.call(second_runtime.read, source.request_id) == unknown


def test_the_original_root_has_only_two_continuations_across_official_restarts(local_env):
    from shared_context.behavior_native_turn_wire import NativeContinuationRead, NativeContinuationSubmit
    from shared_context.native_effects import NativeEffects

    handoffs = []

    def instance(*, block=False):
        sdk = OfflineSdk()
        sdk.block = block
        runtime = NativeTurnRuntime(IDENTITY, sdk)

        async def tracked(receipt, scope, previous, control):
            await NativeEffects(runtime, receipt, control).start_operations()
            return await sdk(receipt, scope, previous, control)

        async def deliver(view):
            handoffs.append(view.request_id)
            original = await runtime.read(view.original_request_id)
            return await runtime.continue_turn(NativeContinuationSubmit(
                command=original.command.model_copy(update={
                    "request_id": view.request_id, "conversation_id": original.conversation_id}),
                capture=original.capture, state_profile=original.state_profile, continuation=view.continuation))

        runtime.executor, runtime.on_continuation = tracked, deliver
        return runtime, sdk

    runtime, sdk = instance()
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        current = Harness(runtime, sdk, client)
        current.invoke(submit("root-base"))
        base = current.wait("root-base", "completed")
        sdk.block = True
        current.invoke(submit("root-two", base.conversation_id))
        source = current.wait("root-two", "started")

    for attempt in (1, 2):
        runtime, sdk = instance(block=True)
        with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
            current = Harness(runtime, sdk, client)
            current.wait(source.request_id, "unknown")
            view = current.invoke(NativeContinuationRead(request_id=source.request_id)).json()
            assert view["continuation"]["root_request_id"] == "root-two"
            assert view["continuation"]["attempt"] == attempt
            following = current.wait(view["request_id"], "started")
            assert following.continuation.continuation_of == source.request_id
            assert current.invoke(NativeContinuationRead(request_id=source.request_id)).json()["request_id"] == following.request_id
            source = following

    runtime, sdk = instance()
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        current = Harness(runtime, sdk, client)
        current.wait(source.request_id, "unknown")
        blocked = current.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        assert blocked["state"] == "blocked" and blocked["failure_code"] == "NATIVE_CONTINUATION_LIMIT"
        assert blocked["request_id"] is None and blocked["root_request_id"] == "root-two"
        assert len(handoffs) == len(set(handoffs)) == 2
        assert sdk.calls == []


@pytest.mark.parametrize("stop_point", [
    "pending", "observation-race", "active", "recovery-race",
    "NATIVE_CONTINUATION_CAPTURE_REVOKED", "WORK_POLICY_REVOKED",
    "NATIVE_CONTINUATION_ADVANCED", "NATIVE_CONTINUATION_WORKSPACE_REQUIRED",
])
def test_stopped_continuation_returns_one_original_outcome_without_rewriting_unknown(
    local_env, stop_point, monkeypatch,
):
    from shared_context.behavior_native_turn_wire import (
        NativeCancel, NativeContinuationRead, NativeContinuationSubmit, NativeRead,
    )
    from shared_context.errors import Problem
    from shared_context.native_effects import NativeEffects
    from shared_context.native_turns import NativePostTurnPending
    from azure.ai.agentserver.core.tasks import RetryPolicy

    first_sdk = OfflineSdk()
    first_runtime = NativeTurnRuntime(IDENTITY, first_sdk)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first_runtime, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    async def unexpected(_view):
        raise AssertionError("No continuation is dispatched before official recovery")

    first_runtime.executor = tracked
    first_runtime.on_continuation = unexpected
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first_runtime)) as client:
        first = Harness(first_runtime, first_sdk, client)
        first.invoke(submit("cancel-base"))
        base = first.wait("cancel-base", "completed")
        first_sdk.block = True
        first.invoke(submit("cancel-root", base.conversation_id))
        source = first.wait("cancel-root", "started")
    sdk = OfflineSdk()
    active = stop_point in {"active", "recovery-race"}
    sdk.block = active
    runtime = NativeTurnRuntime(IDENTITY, sdk, delivery_retry=RetryPolicy(
        max_attempts=3, initial_delay=0.01, max_delay=0.01, jitter=False, retry_on=(NativePostTurnPending,)))
    entered, proceed, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    initial_notification, returned = asyncio.Event(), asyncio.Event()
    outcome = []
    replies = []

    async def account(receipt):
        if receipt.request_id == source.request_id:
            initial_notification.set()

    async def return_outcome(receipt):
        replies.append(receipt)
        returned.set()

    async def delayed(view):
        entered.set()
        if not active:
            await proceed.wait()
        original = await runtime.read(view.original_request_id)
        try:
            if stop_point not in {"pending", "observation-race", "active", "recovery-race"}:
                raise Problem(403 if stop_point.endswith("REVOKED") else 409, stop_point,
                              "OFFLINE continuation admission response")
            return await runtime.continue_turn(NativeContinuationSubmit(
                command=original.command.model_copy(update={
                    "request_id": view.request_id, "conversation_id": original.conversation_id}),
                capture=original.capture, continuation=view.continuation))
        except Problem as error:
            outcome.append(error.body["error"]["code"])
            raise
        finally:
            finished.set()

    runtime.on_continuation = delayed
    runtime.on_terminal, runtime.on_unconfirmed = account, return_outcome
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        current = Harness(runtime, sdk, client)
        current.call(asyncio.wait_for, entered.wait(), 8)
        current.call(asyncio.wait_for, initial_notification.wait(), 8)
        assert replies == []
        unknown = current.call(runtime.read, source.request_id)
        assert unknown.status == "unknown"
        view = current.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        if active:
            child = current.wait(view["request_id"], "started")
        if stop_point == "recovery-race":
            from azure.ai.agentserver.core.tasks import TaskContext
            from shared_context.native_turns import NativeTaskInput

            cancel, raced = runtime.cancel, []

            async def recover_before_cancel(command):
                if command.request_id == child.request_id and not raced:
                    raced.append(True)
                    ctx = TaskContext(
                        task_id=IDENTITY.agent_id, session_id=IDENTITY.session_id,
                        input_id=child.request_id, entry_mode="recovered",
                        input=NativeTaskInput(request_id=child.request_id, accepted_etag="unused-after-start",
                                              call_id="OFFLINE-original-call"))
                    await runtime.handle_turn(ctx)
                return await cancel(command)

            monkeypatch.setattr(runtime, "cancel", recover_before_cancel)
        if stop_point in {"pending", "observation-race"} or active:
            cancelled = current.call(asyncio.wait_for, runtime.cancel(NativeCancel(
                conversation_id=source.conversation_id, request_id=source.request_id)), 2)
            assert cancelled == unknown
        current.call(proceed.set)
        current.call(asyncio.wait_for, finished.wait(), 8)
        assert current.call(runtime.read, source.request_id) == unknown
        if stop_point == "NATIVE_CONTINUATION_WORKSPACE_REQUIRED":
            pending = current.invoke(NativeContinuationRead(request_id=source.request_id)).json()
            assert pending["state"] == "prepared" and not pending.get("cancel_requested", False)
            assert replies == [] and sdk.calls == []
            assert current.invoke(NativeCancel(
                conversation_id=source.conversation_id, request_id=source.request_id)).status_code == 200
        if not active:
            assert outcome == ["NATIVE_TURN_CANCELLED" if stop_point in {"pending", "observation-race"} else stop_point]
            assert current.invoke(NativeRead(request_id=view["request_id"])).status_code == 404
            assert sdk.calls == []
            expected = unknown
        else:
            child = current.wait(view["request_id"], "unknown")
            if stop_point != "recovery-race":
                assert child.failure_code == "NATIVE_TURN_CANCELLED"
            assert [call[0] for call in sdk.calls] == [child.request_id]
            async def stopped():
                while not sdk.controls[child.request_id].stop_requested():
                    await asyncio.sleep(0.01)
            current.call(asyncio.wait_for, stopped(), 2)
            assert outcome == []
            expected = child
        current.call(asyncio.wait_for, returned.wait(), 2)
        assert replies and all(reply == expected for reply in replies)
        stopped = current.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        cancelled_by_owner = stop_point in {
            "pending", "observation-race", "active", "recovery-race", "NATIVE_CONTINUATION_WORKSPACE_REQUIRED",
        }
        assert stopped.get("cancel_requested", False) is cancelled_by_owner
        assert stopped["failure_code"] == ("NATIVE_TURN_CANCELLED" if cancelled_by_owner else stop_point)
        if stop_point == "observation-race":
            from azure.ai.agentserver.core.storage import FoundryStateStore

            read, agent_reads = FoundryStateStore.get_item, []

            async def moving_agent(store, key, **options):
                item = await read(store, key, **options)
                if key == "agent":
                    agent_reads.append(key)
                    return SimpleNamespace(key=item.key, value=item.value, etag=item.etag + str(len(agent_reads)))
                return item

            with monkeypatch.context() as changed:
                changed.setattr(FoundryStateStore, "get_item", moving_agent)
                uncertain = current.invoke(NativeContinuationRead(request_id=source.request_id))
                assert uncertain.status_code == 503
                assert uncertain.json()["error"]["code"] == "NATIVE_CONTINUATION_UNCONFIRMED"
            assert current.invoke(NativeContinuationRead(request_id=source.request_id)).json()["state"] == "blocked"
        assert current.invoke(NativeCancel(
            conversation_id=source.conversation_id, request_id=source.request_id)).status_code == 200
        assert all(reply == expected for reply in replies)


def test_a_continuation_stopped_after_it_started_but_before_it_ran_never_runs_the_sdk(local_env, monkeypatch):
    """The owner stops the root after its continuation is marked started but before that run begins: the run reads
    the stop first, ends the continuation unknown NATIVE_TURN_CANCELLED and never starts the SDK."""
    from azure.ai.agentserver.core.tasks import RetryPolicy
    from shared_context.behavior_native_turn_wire import NativeCancel, NativeContinuationSubmit
    from shared_context.native_effects import NativeEffects
    from shared_context.native_turns import NativePostTurnPending

    first_sdk = OfflineSdk()
    first_runtime = NativeTurnRuntime(IDENTITY, first_sdk)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(first_runtime, receipt, control).start_operations()
        return await first_sdk(receipt, scope, previous, control)

    async def unexpected(_view):
        raise AssertionError("No continuation is dispatched before official recovery")

    first_runtime.executor = tracked
    first_runtime.on_continuation = unexpected
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=first_runtime)) as client:
        first = Harness(first_runtime, first_sdk, client)
        first.invoke(submit("stop-base"))
        base = first.wait("stop-base", "completed")
        first_sdk.block = True
        first.invoke(submit("stop-root", base.conversation_id))
        source = first.wait("stop-root", "started")
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk, delivery_retry=RetryPolicy(
        max_attempts=3, initial_delay=0.01, max_delay=0.01, jitter=False, retry_on=(NativePostTurnPending,)))
    children, stopped = [], []

    async def deliver(view):
        original = await runtime.read(view.original_request_id)
        children.append(view.request_id)
        return await runtime.continue_turn(NativeContinuationSubmit(
            command=original.command.model_copy(update={
                "request_id": view.request_id, "conversation_id": original.conversation_id}),
            capture=original.capture, continuation=view.continuation))

    run_started = runtime._run_started

    async def stop_first(original, control, signals):
        if original.value.receipt.continuation is not None:
            stopped.append(await runtime.cancel(NativeCancel(
                conversation_id=source.conversation_id, request_id=source.request_id)))
        return await run_started(original, control, signals)

    runtime.on_continuation = deliver
    monkeypatch.setattr(runtime, "_run_started", stop_first)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        current = Harness(runtime, sdk, client)

        async def child_ran():
            while not stopped:
                await asyncio.sleep(0.01)

        current.call(asyncio.wait_for, child_ran(), 8)
        child = current.wait(children[0], "unknown")
        assert child.failure_code == "NATIVE_TURN_CANCELLED"
        assert child.continuation is not None and sdk.calls == []
        assert [receipt.request_id for receipt in stopped] == [source.request_id]


@pytest.mark.parametrize("applied", [False, True])
def test_recovery_intent_response_loss_keeps_one_child_and_the_original_unknown(local_env, monkeypatch, applied):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError
    from shared_context.behavior_native_turn_wire import NativeContinuationRead, NativeContinuationSubmit
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)

    async def tracked(receipt, scope, previous, control):
        await NativeEffects(runtime, receipt, control).start_operations()
        return await sdk(receipt, scope, previous, control)

    runtime.executor = tracked
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        first = Harness(runtime, sdk, client)
        first.invoke(submit("intent-base"))
        base = first.wait("intent-base", "completed")
        sdk.block = True
        first.invoke(submit("intent-root", base.conversation_id))
        source = first.wait("intent-root", "started")

    lost = []
    create, write = FoundryStateStore.create_item, FoundryStateStore.set_item

    def is_intent(value):
        value = value.get("continuation_intent", value)
        return isinstance(value, dict) and value.get("kind") == "fa-native-continuation-intent-v1"

    async def create_unknown(store, key, value, **options):
        if is_intent(value) and not lost:
            lost.append(key)
            if applied:
                await create(store, key, value, **options)
            raise ServiceResponseError("OFFLINE original intent response lost")
        return await create(store, key, value, **options)

    async def write_unknown(store, key, value, **options):
        if is_intent(value) and not lost:
            lost.append(key)
            if applied:
                await write(store, key, value, **options)
            raise ServiceResponseError("OFFLINE original intent response lost")
        return await write(store, key, value, **options)

    monkeypatch.setattr(FoundryStateStore, "create_item", create_unknown)
    monkeypatch.setattr(FoundryStateStore, "set_item", write_unknown)
    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    delivered = []
    returned = asyncio.Event()
    outcomes = []

    async def return_outcome(receipt):
        outcomes.append(receipt)
        returned.set()

    async def dispatch(view):
        original = await runtime.read(view.original_request_id)
        delivered.append(view.request_id)
        return await runtime.continue_turn(NativeContinuationSubmit(
            command=original.command.model_copy(update={
                "request_id": view.request_id, "conversation_id": original.conversation_id}),
            capture=original.capture, continuation=view.continuation))

    runtime.on_continuation = dispatch
    runtime.on_unconfirmed = return_outcome
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        current = Harness(runtime, sdk, client)
        unknown = current.wait(source.request_id, "unknown")
        observed = current.invoke(NativeContinuationRead(request_id=source.request_id))
        if not applied:
            assert len(lost) == 1 and unknown.failure_code == "NATIVE_CONTINUATION_UNCONFIRMED"
            assert observed.status_code == 404 and delivered == [] and sdk.calls == []
            current.call(asyncio.wait_for, returned.wait(), 2)
            assert outcomes == [unknown]
            return
        view = observed.json()
        child = current.wait(view["request_id"], "completed")
        assert len(lost) == 1 and delivered == [child.request_id]
        assert child.continuation.attempt == 1
        assert current.call(runtime.read, source.request_id) == unknown
        assert len(sdk.calls) == 1


@pytest.mark.parametrize("missing", ["checkpoint", "watermark"])
def test_official_recovery_stops_without_the_required_original_material(local_env, missing):
    from shared_context.behavior_native_turn_wire import NativeContinuationRead
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)

    async def tracked(receipt, scope, previous, control):
        if missing != "watermark":
            await NativeEffects(runtime, receipt, control).start_operations()
        return await sdk(receipt, scope, previous, control)

    runtime.executor = tracked
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        first = Harness(runtime, sdk, client)
        cid = None
        if missing == "watermark":
            assert first.invoke(submit("material-base")).status_code == 202
            cid = first.wait("material-base", "completed").conversation_id
        sdk.block = True
        assert first.invoke(submit("material-root", cid)).status_code == 202
        source = first.wait("material-root", "started")

    resumed_sdk = OfflineSdk()
    resumed = NativeTurnRuntime(IDENTITY, resumed_sdk)
    handoffs = []

    async def unexpected(view):
        handoffs.append(view.request_id)
        raise AssertionError("Missing recovery material must not dispatch another input")

    resumed.on_continuation = unexpected
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=resumed)) as client:
        current = Harness(resumed, resumed_sdk, client)
        original = current.wait(source.request_id, "unknown")
        view = current.invoke(NativeContinuationRead(request_id=source.request_id)).json()
        assert view["state"] == "blocked" and view["request_id"] is None
        assert view["failure_code"] == {
            "checkpoint": "NATIVE_CONTINUATION_CHECKPOINT_REQUIRED",
            "watermark": "NATIVE_CONTINUATION_WATERMARK_UNAVAILABLE",
        }[missing]
        assert current.call(resumed.read, source.request_id) == original
        assert resumed_sdk.calls == [] and handoffs == []


def test_normal_failure_reads_and_duplicate_submit_never_turn_historical_unknown_into_recovery(local_env):
    from shared_context.behavior_native_turn_wire import NativeContinuationRead, NativeRead
    from shared_context.native_effects import NativeEffects

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk)
    executions, handoffs = [], []

    async def normal_failure(receipt, scope, previous, control):
        executions.append(receipt.request_id)
        await NativeEffects(runtime, receipt, control).start_operations()
        if receipt.request_id == "normal-failure":
            raise OSError("OFFLINE model transport failed, without process loss")
        return await sdk(receipt, scope, previous, control)

    async def unexpected(view):
        handoffs.append(view.request_id)
        raise AssertionError("A historical UNKNOWN must not create a recovery input")

    runtime.executor, runtime.on_continuation = normal_failure, unexpected
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        current = Harness(runtime, sdk, client)
        assert current.invoke(submit("normal-base")).status_code == 202
        base = current.wait("normal-base", "completed")
        command = submit("normal-failure", base.conversation_id)
        assert current.invoke(command).status_code == 202
        original = current.wait(command.command.request_id, "unknown")
        assert original.continuation_available
        for _ in range(2):
            assert current.invoke(NativeRead(request_id=original.request_id)).json() == original.model_dump(mode="json")
            assert current.invoke(command).json() == original.model_dump(mode="json")
            assert current.invoke(NativeContinuationRead(request_id=original.request_id)).status_code == 404
        assert executions == ["normal-base", "normal-failure"] and handoffs == []

    resumed_sdk = OfflineSdk()
    resumed = NativeTurnRuntime(IDENTITY, resumed_sdk)
    resumed.on_continuation = unexpected
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=resumed)) as client:
        current = Harness(resumed, resumed_sdk, client)
        assert current.invoke(command).json() == original.model_dump(mode="json")
        assert current.invoke(NativeContinuationRead(request_id=original.request_id)).status_code == 404
        assert resumed_sdk.calls == [] and handoffs == []


@pytest.mark.parametrize("unavailable_reads", [1, 3])
def test_terminal_handoff_read_failure_retries_or_retains_visible_notification_unknown(
    local_env, monkeypatch, unavailable_reads,
):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.ai.agentserver.core.tasks import RetryPolicy
    from azure.core.exceptions import ServiceResponseError
    from shared_context.native_turns import NativePostTurnPending

    sdk = OfflineSdk()
    runtime = NativeTurnRuntime(IDENTITY, sdk, delivery_retry=RetryPolicy(
        max_attempts=3, initial_delay=0.01, max_delay=0.01, jitter=False, retry_on=(NativePostTurnPending,)))
    reads, outcomes = [], []
    read = FoundryStateStore.get_item

    async def unavailable(store, key, **options):
        if key.startswith("continuation:"):
            reads.append(key)
            if len(reads) <= unavailable_reads:
                raise ServiceResponseError("OFFLINE handoff metadata temporarily unavailable")
        return await read(store, key, **options)

    async def unexpected(_view):
        raise AssertionError("A completed original turn does not create a continuation")

    async def terminal(receipt):
        outcomes.append(receipt)

    monkeypatch.setattr(FoundryStateStore, "get_item", unavailable)
    runtime.on_continuation, runtime.on_terminal = unexpected, terminal
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        current = Harness(runtime, sdk, client)
        assert current.invoke(submit("notification-original")).status_code == 202
        original = current.wait("notification-original", "completed")

        async def observed():
            for _ in range(100):
                value = await runtime.read(original.request_id)
                if outcomes or value.notification_error == "NATIVE_POST_TURN_UNKNOWN":
                    return value
                await asyncio.sleep(0.01)
            raise AssertionError("the transient handoff read neither retried nor retained notification UNKNOWN")

        result = current.call(observed)
        assert result.status == "completed" and result.result == original.result
        assert len(sdk.calls) == 1
        if unavailable_reads == 1:
            assert len(reads) == 2 and outcomes == [original]
            assert result.notification_error is None
        else:
            assert len(reads) == 3 and outcomes == []
            assert result.notification_error == "NATIVE_POST_TURN_UNKNOWN"
