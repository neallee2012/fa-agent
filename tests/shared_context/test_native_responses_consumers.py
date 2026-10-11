"""N6-1b: the unchanged native consumers on the official Responses lane; only the SDK and memory service are fakes."""

from contextlib import asynccontextmanager
import json
import time
from types import SimpleNamespace

import pytest
from azure.ai.agentserver.invocations import InvocationAgentServerHost
from azure.ai.agentserver.responses import ResponsesAgentServerHost, ResponsesServerOptions
from starlette.testclient import TestClient

from shared_context import native_memory_candidate as candidates
from shared_context import native_private_memory as native_memory
from shared_context import native_sdk
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NativeCandidateRead
from shared_context.native_responses import native_responses_handler_factory
from shared_context.native_responses_wire import NATIVE_RESPONSES_PROFILE, NATIVE_STATE_PROFILE
from shared_context.native_sdk_profiles import native_context
from shared_context.native_turns import NativeTurnRuntime, native_response
from test_native_capabilities import selected_capture
from test_native_memory_candidate import QUOTE, TEXT, stage_args
from test_native_private_memory import ENDPOINT, actions, private
from test_native_responses import ResponsesHarness
from test_native_turn_wire import submit
from test_native_turns import IDENTITY, local_env  # noqa: F401  (pytest fixture)


def responses_submit(rid="rid-1"):
    values = {"command": submit(rid, None, TEXT).command.model_dump(mode="json"),
              "capture": selected_capture(candidates.ENTRY_ID).model_dump(mode="json"),
              "state_profile": NATIVE_RESPONSES_PROFILE, "new_conversation_id": "responses-" + rid}
    return NATIVE_REQUEST.validate_json(json.dumps({"operation": "submit", **values}))


def responses_host(runtime):
    class Host(InvocationAgentServerHost, ResponsesAgentServerHost):
        pass

    app = Host(options=ResponsesServerOptions(resilient_background=True, steerable_conversations=False),
               configure_observability=None, access_log=None)
    app.response_handler(native_responses_handler_factory(SimpleNamespace(identity=IDENTITY), runtime))
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def initialize(application):
        await runtime.initialize()
        async with lifespan(application):
            yield

    app.router.lifespan_context = initialize

    @app.invoke_handler
    async def invoke(request):
        return await native_response(request, runtime)
    return app


def test_a_responses_human_turn_keeps_recall_one_update_and_the_candidate_on_the_server_selected_cid(
        local_env, monkeypatch, tmp_path):
    sent, prompts, staged = [], [], []
    memories = private(sent)

    async def sdk(**options):
        prompts.append(options["prompt"])
        spec = next(tool for tool in options["tools"] if tool.name == candidates.TOOL)
        output = await spec.handler({"session_id": options["sdk_session_id"], "tool_call_id": "OFFLINE-call-0",
                                     "tool_name": spec.name, "arguments": stage_args()})
        staged.append(json.loads(output.text))
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE answer",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=lambda: "OFFLINE-model-token",
        capability_providers=(candidates.PROVIDER,), private_memory=memories)

    async def completed(receipt, _result):
        await memories.completed(receipt)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, on_completed=completed)
    executor.reader = runtime  # native_runtime_from_environment wires both native lanes to this one runtime
    request = responses_submit()
    with TestClient(responses_host(runtime)) as client:
        h = ResponsesHarness(runtime, SimpleNamespace(controls={}), client)
        assert h.invoke(request).status_code == 202
        accepted = h.call(runtime.read, "rid-1")
        assert (accepted.state_profile, accepted.conversation_id) == (NATIVE_RESPONSES_PROFILE, "responses-rid-1")
        assert h.respond(accepted).status_code == 200
        receipt = h.wait("rid-1", "completed")
        deadline = time.monotonic() + 8
        while len(sent) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        review = h.call(runtime.dispatch, NativeCandidateRead(conversation_id=receipt.conversation_id,
                                                              request_id="rid-1"))
        # The official terminal-notification retry never resends the private update for the same RID.
        h.call(memories.completed, receipt)

    assert prompts == [native_memory.PREFIX + "\n- The cat is Migao, 3 years old\n\n" + TEXT]
    assert sorted(actions(sent)) == ["search_memories", "search_memories", "update_memories"]
    assert receipt.response_id is not None and receipt.source is None
    assert staged[0]["state"] == "staged" and staged[0]["conversation_id"] == "responses-rid-1"
    assert (review.state, review.text, review.effective) == ("pending_review", QUOTE, False)
    assert review.source.conversation_id == "responses-rid-1" and review.source.original_request_id == "rid-1"


@pytest.mark.parametrize("profile", [NATIVE_STATE_PROFILE, NATIVE_RESPONSES_PROFILE])
def test_read_context_reports_the_receipts_own_lane(profile):
    from shared_context.behavior_native_turn_wire import NativeSubmit
    from test_native_turn_wire import capture, receipt_for, submit
    request = NativeSubmit(command=submit("rid-1", "cid-1", text=TEXT).command, capture=capture(), state_profile=profile)
    receipt = receipt_for(request)
    context = native_context(receipt, None)
    assert context.state_profile == profile
    assert context.turn_origin.source_kind == "human" and context.turn_origin.continuation is None
    # The old lane's default stays the exact value it always serialized.
    assert json.loads(context.model_dump_json())["state_profile"] == profile
