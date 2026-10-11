"""ADR-0007 C5: a native human turn's inactive memory suggestion and the owner's review read.

Offline: the real native executor seam and official local chain/store; SDK and model I/O are named doubles.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ServiceResponseError
from starlette.testclient import TestClient

from shared_context import native_memory_candidate as candidates
from shared_context import native_sdk
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_api import NativeTurnGateway
from shared_context.behavior_native_turn_wire import NativeCandidateRead, NativeSubmit
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from shared_context.memory_candidate_wire import CandidateCommand, NativeCandidateView
from shared_context.native_checkpoint import conversation_store_name, open_durable_store
from shared_context.native_turns import NativeTurnRuntime
from test_native_capabilities import selected_capture
from test_native_turn_wire import agent_receive, receipt_for, submit
from test_native_turns import Harness, IDENTITY, local_env

TEXT = "Please remember: my cat is Migao. Thanks!"
QUOTE = "my cat is Migao"


def stage_args(decision="candidate", quote=QUOTE, reason="durable_fact"):
    return {"decision": decision, "reason": reason, **({"quote": quote} if quote is not None else {})}


def command(**changes):
    return CandidateCommand(request_id="stage-memory-candidate", **stage_args(**changes))


def run_turn(local_env, monkeypatch, tmp_path, calls, *, text=TEXT):
    """One human native turn whose SDK double calls stage_memory_candidate with each of ``calls``."""
    outputs = []

    async def sdk(**options):
        spec = next(tool for tool in options["tools"] if tool.name == candidates.TOOL)
        for index, arguments in enumerate(calls):
            packet = {"session_id": options["sdk_session_id"], "tool_call_id": f"OFFLINE-call-{index}",
                      "tool_name": spec.name, "arguments": arguments}
            try:
                output = await spec.handler(packet)
                assert options["behavior_boundary"].tool_result(spec.name, output.text) == output.text
                outputs.append(json.loads(output.text))
            except ToolFailure as failure:
                outputs.append(failure.public_code)
        receipt = runtime.executor.reader
        original = await receipt.read("rid-1")
        outputs.append(await receipt.dispatch(NativeCandidateRead(
            conversation_id=original.conversation_id, request_id="rid-1")))
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE answer",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-token", capability_providers=(candidates.PROVIDER,))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    request = NativeSubmit(command=submit(text=text).command, capture=selected_capture(candidates.ENTRY_ID))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(request).status_code == 202
        receipt = harness.wait("rid-1", "completed")
        review = harness.call(runtime.dispatch, NativeCandidateRead(
            conversation_id=receipt.conversation_id, request_id="rid-1"))
    return SimpleNamespace(outputs=outputs, receipt=receipt, review=review, runtime=runtime)


def test_a_human_turn_stages_one_exact_suggestion_shown_only_after_completion(local_env, monkeypatch, tmp_path):
    turn = run_turn(local_env, monkeypatch, tmp_path, [stage_args(), stage_args(), stage_args("no_candidate", None,
                                                                                               "no_durable_information")])
    staged = {"schema_version": "fa-native-memory-candidate-receipt-v1", "agent_id": IDENTITY.agent_id,
              "conversation_id": turn.receipt.conversation_id, "request_id": "rid-1",
              "decision": "candidate", "state": "staged", "effective": False}
    # The same decision returns its original receipt; another decision for the turn is a conflict.
    assert turn.outputs[:3] == [staged, staged, "MEMORY_CANDIDATE_CONFLICT"]
    assert turn.outputs[3].state == "awaiting_saved_turn" and turn.outputs[3].text is None
    review = turn.review
    assert review.state == "pending_review" and review.effective is False and review.text == QUOTE
    start = TEXT.encode().index(QUOTE.encode())
    assert (review.source.start_byte, review.source.end_byte) == (start, start + len(QUOTE.encode()))
    assert review.source.original_request_id == "rid-1" and review.turn_number == turn.receipt.turn_number


def test_a_skip_keeps_no_text_and_a_quote_must_be_exact_and_unique(local_env, monkeypatch, tmp_path):
    turn = run_turn(local_env, monkeypatch, tmp_path, [
        stage_args(quote="not in this message"), stage_args(quote="Migao Migao".split()[0]),
        stage_args("no_candidate", None, "no_durable_information")], text="Migao and Migao again")
    assert turn.outputs[:2] == ["MEMORY_CANDIDATE_SOURCE_INVALID", "MEMORY_CANDIDATE_SOURCE_INVALID"]
    assert turn.outputs[2]["decision"] == "no_candidate"
    assert turn.review.state == "skipped" and turn.review.text is None and turn.review.source is None


def test_a_changed_stored_suggestion_is_refused_not_shown(local_env, monkeypatch, tmp_path):
    turn = run_turn(local_env, monkeypatch, tmp_path, [stage_args()])

    async def tamper():
        async with await open_durable_store(conversation_store_name(turn.receipt.conversation_id), create=False) as store:
            key = candidates.candidate_key("rid-1")
            item = await store.get_item(key)
            value = json.loads(json.dumps(item.value))
            value["command"]["quote"] = "Thanks!"
            await store.set_item(key, value)
    asyncio.run(tamper())
    with pytest.raises(Problem) as refused:
        asyncio.run(turn.runtime.dispatch(NativeCandidateRead(
            conversation_id=turn.receipt.conversation_id, request_id="rid-1")))
    assert refused.value.body["error"]["code"] == "MEMORY_CANDIDATE_INVALID"
    with pytest.raises(Problem) as other:
        asyncio.run(turn.runtime.dispatch(NativeCandidateRead(conversation_id="another-cid", request_id="rid-1")))
    assert other.value.body["error"]["code"] == "NATIVE_STATE_SCOPE"


def receipt(source=None, request_id="rid-1"):
    request = (submit(request_id, "cid-1", text=TEXT) if source is None else
               agent_receive(request_id, "cid-1"))
    return receipt_for(request)


class LocalRuntime:
    """The real local State store behind the runtime's private store opener, with chosen store failures."""

    def __init__(self, *, lose_ack=False, fail_write=False):
        self.lose_ack, self.fail_write = lose_ack, fail_write

    @asynccontextmanager
    async def _store(self, name, *, create=False):
        async with await open_durable_store(name, create=True) as store:
            runtime = self

            class Store:
                async def create_item(self, key, value):
                    if not runtime.fail_write:
                        await store.create_item(key, value)
                    if runtime.lose_ack or runtime.fail_write:
                        raise ServiceResponseError("OFFLINE response lost")

                async def get_item(self, key):
                    return await store.get_item(key)
            yield Store()


def test_another_agent_never_counts_as_the_user(local_env):
    with pytest.raises(Problem) as refused:
        asyncio.run(candidates.stage(LocalRuntime(), receipt(source=object()), command()))
    assert refused.value.body["error"]["code"] == "MEMORY_CANDIDATE_DISABLED"


def test_a_lost_acknowledgment_reads_back_the_same_key_and_an_unwritten_one_is_unknown(local_env):
    staged = asyncio.run(candidates.stage(LocalRuntime(lose_ack=True), receipt(), command()))
    assert staged.decision == "candidate" and staged.request_id == "rid-1"
    with pytest.raises(Problem) as conflict:
        asyncio.run(candidates.stage(LocalRuntime(lose_ack=True), receipt(), command(quote="cat")))
    assert conflict.value.body["error"]["code"] == "MEMORY_CANDIDATE_CONFLICT"
    with pytest.raises(Problem) as unknown:
        asyncio.run(candidates.stage(LocalRuntime(fail_write=True), receipt(request_id="rid-2"), command()))
    assert (unknown.value.status, unknown.value.body["error"]["code"]) == (503, "BEHAVIOR_COMMIT_UNKNOWN")


def test_a_record_without_a_correction_keeps_its_earlier_bytes_and_a_correction_is_one_decision(local_env):
    """MEM-02: ``supersedes`` is written only when present, so earlier records read unchanged."""
    from shared_context.native_turns import _decode, _json

    plain = candidates._record(receipt(), command())
    assert "supersedes" not in _json(plain) and "supersedes" not in plain.model_dump_json()
    corrected = candidates._record(receipt(), command(), "mem-old-0001")
    assert _json(corrected)["supersedes"] == "mem-old-0001"
    assert _decode(_json(corrected), candidates.NativeCandidateRecord) == corrected
    assert _decode(_json(plain), candidates.NativeCandidateRecord) == plain
    with pytest.raises(ValueError):
        candidates._record(receipt(), command(decision="no_candidate", quote=None, reason="no_durable_information"),
                           "mem-old-0001")
    with pytest.raises(ValueError):
        candidates.NativeCandidateArguments(decision="no_candidate", reason="no_durable_information",
                                            supersedes="note:t1")
    staged = asyncio.run(candidates.stage(LocalRuntime(), receipt(), command(), "mem-old-0001"))
    assert staged.decision == "candidate"
    for other in (None, "mem-old-0002"):
        with pytest.raises(Problem) as conflict:
            asyncio.run(candidates.stage(LocalRuntime(), receipt(), command(), other))
        assert conflict.value.body["error"]["code"] == "MEMORY_CANDIDATE_CONFLICT"


def test_only_the_exact_builtin_grant_is_accepted_and_the_factory_registers_it():
    from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES, _grant
    grant = _grant(BUILTIN_CAPABILITIES[candidates.ENTRY_ID])
    assert candidates.accepts(grant)
    assert not candidates.accepts(grant.model_copy(update={"entry_sha256": "0" * 64}))
    assert not candidates.accepts(_grant(BUILTIN_CAPABILITIES["fa-shell-v1"]))
    assert candidates.PROVIDER in native_sdk.native_capability_providers("https://api.offline.invalid", None)


def test_the_api_gateway_reads_the_view_only_for_the_original_agent_and_request():
    view = NativeCandidateView(agent_id="agent-1", conversation_id="cid-1", request_id="rid-1", state="not_recorded")

    def client(value):
        response = SimpleNamespace(body=value.model_dump_json().encode(), status_code=200, invocation_id="OFFLINE")
        return SimpleNamespace(invoke_native=lambda _command: response, target=SimpleNamespace(agent_id="agent-1"))
    read = NativeCandidateRead(conversation_id="cid-1", request_id="rid-1")
    assert NativeTurnGateway._invoke(client(view), read)[0] == view
    for changed in ({"agent_id": "agent-2"}, {"request_id": "rid-2"}, {"conversation_id": "cid-2"}):
        with pytest.raises(Problem) as refused:
            NativeTurnGateway._invoke(client(view.model_copy(update=changed)), read)
        assert refused.value.body["error"]["code"] == "NATIVE_STATE_SCOPE"
