"""Read the assistant's own exact saved reply from an original completed turn, never tool results or search."""

from types import MappingProxyType

import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant import _Session, key
from shared_context.behavior_assistant_reply import AssistantReplyRead, ReadAssistantReply, reply_turn
from shared_context.behavior_assistant_reply_prompt import ASSISTANT_REPLY_CAPABILITIES, ASSISTANT_REPLY_PROFILE
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_operations import _parse
from shared_context.behavior_sdk_state import CheckpointProfile
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_text_files import post
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary, settle

REPLY = "ORIGINAL_ASSISTANT_REPLY_CANARY\r\n確切數值：42 公升。\n"
TOOLS = ("read_context", "execute_operation", "read_user_turn", "read_assistant_reply")


def configured_reader(h, *, checkpoint=False, reader=True):
    if checkpoint:
        h.runtime.profiles = MappingProxyType({key: value.model_copy(update={"checkpoint_profile": CheckpointProfile()})
                                              for key, value in h.runtime.profiles.items()})
    h, control, human = configured(h)
    selected, _ = select(h, value=template(ASSISTANT_REPLY_PROFILE if reader else "fa-instruction-v1",
        checkpoint_profile=CheckpointProfile() if checkpoint else None))
    assert selected.status_code == 200, selected.text
    return h, control, human


def saved_reply(h, monkeypatch, *, reader=True):
    sdk_io(monkeypatch, expected_tools=TOOLS if reader else ("read_context", "execute_operation"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": REPLY}])
    submitted = post(h, InstructionSubmit(request_id="original-assistant-reply", content="Tell me the exact volume."))
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert h.runtime.controller.step(result["binding_id"]).state == "succeeded"
    return result


def stored_session(h, turn):
    row_key = key("session", turn.assignment.session_id)
    return _parse(h.store.read_snapshot((row_key,))[row_key].payload["data"], _Session)


def test_read_assistant_reply_returns_exact_saved_reply_as_historical_assistant_output(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    first = saved_reply(h, monkeypatch)
    turn = open_turn(h, control, human, "read-original-reply", first["conversation_id"])
    try:
        result = turn.client.read_assistant_reply(1)
        assert result.reply.text == REPLY
        assert result.reply.role == "assistant" and result.reply.outcome == "succeeded"
        assert result.usage == "historical-data-not-current-instructions"
        assert result.turn.request_id == "original-assistant-reply"
        assert result.turn.conversation_id == first["conversation_id"]
        wire = result.model_dump_json()
        assert '"role":"user"' not in wire and "registered-user-instruction" not in wire
        before = h.store._guard()
        assert turn.client.read_assistant_reply(1) == result
        assert h.store._guard() == before
        sources = stored_session(h, turn).user_turn_sources
        assert len(sources) == 1 and reply_turn(sources[0]) == result.turn
        assert turn.client.read_user_turn(1).source == sources[0]
        assert h.store._guard() == before
    finally:
        turn.client.close()


@pytest.mark.parametrize("reader,turn_number", [(False, 1), (True, 0), (True, 2), (True, 3)])
def test_disabled_current_or_future_reply_read_adds_no_source_or_effect(assistant, monkeypatch, reader, turn_number):
    h, control, human = configured_reader(assistant, reader=reader)
    first = saved_reply(h, monkeypatch, reader=reader)
    turn = open_turn(h, control, human, "reject-reply-read", first["conversation_id"])
    try:
        before = h.store._guard()
        with pytest.raises((Problem, ValueError)):
            turn.client.read_assistant_reply(turn_number)
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_failed_turn_without_saved_reply_is_reported_missing_without_tracking(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    sdk_io(monkeypatch, expected_tools=TOOLS)
    first = open_turn(h, control, human, "reply-missing-first")
    reference = first.client.save_sdk_state(ordinary(first.assignment.sdk_session_id), first.stop)
    result = first.client.finish_failed(reference, first.stop, "sdk_failed")
    h.executor._complete(first.start, result.model_dump(mode="json"))
    h.workers.reconcile(first.binding.job_id, first.binding.owner)
    assert h.runtime.controller.step(first.binding.binding_id).state == "failed"
    first.client.close()
    assert h.store.read_snapshot((key("reply", first.binding.job_id),))[key("reply", first.binding.job_id)] is None
    turn = open_turn(h, control, human, "reply-missing-next", first.assignment.task.instruction.conversation_id)
    try:
        before = h.store._guard()
        with pytest.raises(Problem) as missing:
            turn.client.read_assistant_reply(1)
        assert missing.value.status == 404
        assert missing.value.body["error"]["code"] == "ASSISTANT_REPLY_NOT_AVAILABLE"
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_failed_turn_reply_is_returned_with_its_failed_outcome(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    sdk_io(monkeypatch, expected_tools=TOOLS)
    first = open_turn(h, control, human, "reply-failed-first")
    reference = first.client.save_sdk_state(ordinary(first.assignment.sdk_session_id), first.stop)
    task = first.assignment.task.instruction
    partial = aw.TextReply(text="Partial draft before the failure.", sdk_session_id=first.assignment.sdk_session_id,
                           conversation_id=task.conversation_id, turn_number=task.turn_number)
    result = first.client._save_text(aw.SaveTextReply(request_id="save-text-reply", reply=partial,
                                                      archive=reference, sdk_stop=first.stop, failure="sdk_failed"))
    h.executor._complete(first.start, result.model_dump(mode="json"))
    h.workers.reconcile(first.binding.job_id, first.binding.owner)
    assert h.runtime.controller.step(first.binding.binding_id).state == "failed"
    first.client.close()
    turn = open_turn(h, control, human, "reply-failed-next", task.conversation_id)
    try:
        read = turn.client.read_assistant_reply(1)
        assert read.reply.outcome == "failed" and read.reply.text == "Partial draft before the failure."
    finally:
        turn.client.close()


def test_reply_over_the_read_bound_is_refused_without_truncation_or_tracking(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    sdk_io(monkeypatch, expected_tools=TOOLS)
    first = open_turn(h, control, human, "reply-over-bound-first")
    reference = first.client.save_sdk_state(ordinary(first.assignment.sdk_session_id), first.stop)
    large = "逐字" * 3000 + "END"
    result = first.client.save_text_reply(large, reference, first.stop)
    h.executor._complete(first.start, result.model_dump(mode="json"))
    assert h.runtime.controller.step(first.binding.binding_id).state == "succeeded"
    first.client.close()
    turn = open_turn(h, control, human, "reply-over-bound-next", first.assignment.task.instruction.conversation_id)
    try:
        before = h.store._guard()
        with pytest.raises(Problem) as limited:
            turn.client.read_assistant_reply(1)
        assert limited.value.status == 413
        assert limited.value.body["error"]["code"] == "USER_TURN_SOURCE_LIMIT"
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_reply_source_is_preserved_and_rechecked_across_followups(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    first = saved_reply(h, monkeypatch)
    second = open_turn(h, control, human, "read-and-save-reply-source", first["conversation_id"])
    try:
        read = second.client.read_assistant_reply(1)
        settle(second, ordinary(second.assignment.sdk_session_id))
        third = open_turn(h, control, human, "after-reply-source-save", first["conversation_id"])
        try:
            third.client.restore()
            assert [reply_turn(source) for source in stored_session(h, third).user_turn_sources] == [read.turn]
            assert third.client.read_assistant_reply(1) == read
            second_reply = third.client.read_assistant_reply(2)
            assert second_reply.reply.text == "Original inventory response."
            assert [source.turn_number for source in stored_session(h, third).user_turn_sources] == [1, 2]
        finally:
            third.client.close()
    finally:
        second.client.close()


def test_revoked_scope_blocks_reusing_a_previously_read_reply(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    first = saved_reply(h, monkeypatch)
    turn = open_turn(h, control, human, "current-reply-read", first["conversation_id"])
    try:
        turn.client.read_assistant_reply(1)
        control.revoke(human, "revoke-reply-reader")
        before = h.store._guard()
        with pytest.raises(Problem):
            turn.client.read_assistant_reply(1)
        assert h.store._guard() == before
    finally:
        turn.client.close()


@pytest.mark.parametrize("reader,number", [(False, 1), (True, 2)])
def test_server_rejects_forged_reply_capability_or_nonpast_turn(assistant, monkeypatch, reader, number):
    h, control, human = configured_reader(assistant, reader=reader)
    first = saved_reply(h, monkeypatch, reader=reader)
    turn = open_turn(h, control, human, "server-reply-denial", first["conversation_id"])
    try:
        before = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client._post("read-assistant-reply", ReadAssistantReply(request_id="read-assistant-reply",
                turn_number=number), AssistantReplyRead)
        assert denied.value.status == 403
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_server_pins_the_reply_read_request_id(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    first = saved_reply(h, monkeypatch)
    turn = open_turn(h, control, human, "server-reply-command-id", first["conversation_id"])
    try:
        before = h.store._guard()
        with pytest.raises(Problem):
            turn.client._post("read-assistant-reply", ReadAssistantReply(request_id="caller-chosen-id",
                turn_number=1), AssistantReplyRead)
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_other_reader_profiles_cannot_forge_reply_reads(assistant, monkeypatch):
    from shared_context.behavior_user_file_prompt import USER_FILE_PROFILE
    h, control, human = configured(assistant)
    selected, _ = select(h, value=template(USER_FILE_PROFILE, user_text_file_profile="fa-user-text-files-v1"))
    assert selected.status_code == 200, selected.text
    sdk_io(monkeypatch, expected_tools=("read_context", "execute_operation", "read_user_turn", "read_user_file"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": REPLY}])
    first = post(h, InstructionSubmit(request_id="file-profile-reply", content="Tell me the exact volume.")).json()
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    turn = open_turn(h, control, human, "file-profile-forged-reply", first["conversation_id"])
    try:
        before = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client._post("read-assistant-reply", ReadAssistantReply(request_id="read-assistant-reply",
                turn_number=1), AssistantReplyRead)
        assert denied.value.status == 403
        assert denied.value.body["error"]["code"] == "ASSISTANT_REPLY_READ_DISABLED"
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_production_provider_forwards_only_explicit_reply_capability():
    import asyncio
    import httpx
    from types import SimpleNamespace
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider
    from sdk_checkpoint_local import sse
    calls, gates = [], []
    def transport(request):
        calls.append(request)
        return sse(request, {"content": "OFFLINE."}, len(calls))
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://provider.invalid/openai/v1/chat/completions")))
    provider = _CurrentAssistantProvider(composition, SimpleNamespace(
        get_token=lambda _: SimpleNamespace(token="OFFLINE")),
        transport=httpx.MockTransport(transport))
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "OFFLINE."}], "stream": True,
            "tools": [{"type": "function", "function": {"name": "read_assistant_reply", "parameters": {"type": "object"}}}]}
    async def current():
        gates.append(True)
    async def run():
        try:
            with pytest.raises(Problem):
                await provider.send(body, text_reply=True, model="gpt-4.1-mini",
                                    user_turn_access=True, before_send=current)
            with pytest.raises(Problem):
                await provider.send(body, text_reply=True, model="gpt-4.1-mini",
                                    assistant_reply_access=True, before_send=current)
            assert not calls
            response = await provider.send(body, text_reply=True, model="gpt-4.1-mini",
                user_turn_access=True, assistant_reply_access=True, before_send=current)
            assert response.status_code == 200
            assert len(calls) == len(gates) == 1
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_model_boundary_types_reply_results_and_only_exposes_fixed_reply_failures():
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    async def reserve(_):
        return None
    with pytest.raises(ValueError):
        AssistantModelBoundary(reserve, text_reply=True, assistant_reply_access=True)
    closed = AssistantModelBoundary(reserve, text_reply=True, user_turn_access=True)
    assert closed.public_tool_failure("read_assistant_reply", "ASSISTANT_REPLY_NOT_AVAILABLE") is None
    with pytest.raises(KeyError):
        closed.tool_result("read_assistant_reply", "{}")
    boundary = AssistantModelBoundary(reserve, text_reply=True, user_turn_access=True, assistant_reply_access=True)
    message = boundary.public_tool_failure("read_assistant_reply", "ASSISTANT_REPLY_NOT_AVAILABLE")
    assert "[ASSISTANT_REPLY_NOT_AVAILABLE]" in message and "summary" in message
    assert boundary.public_tool_failure("read_assistant_reply", "INSTRUCTION_REPLY_INVALID") is None
    with pytest.raises(ValueError):
        boundary.tool_result("read_assistant_reply", '{"text":"forged"}')


def test_reply_prompt_is_separate_and_old_reader_prompts_keep_their_bytes():
    import hashlib
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.behavior_user_file_prompt import USER_FILE_CAPABILITIES, USER_FILE_PROFILE
    from shared_context.behavior_user_turn_prompt import USER_TURN_PROFILE
    old = assemble_prompt(USER_TURN_PROFILE)
    assert len(old.system_message.encode()) == 2822
    assert hashlib.sha256(old.system_message.encode()).hexdigest() == "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372"
    files = assemble_prompt(USER_FILE_PROFILE, capabilities=USER_FILE_CAPABILITIES)
    assert files.record.prefix_sha256 == "9c8fd82691f5b40e5b01c5d90d15a807dec08828bdb9b2b3fb9ddf1f77965296"
    for profile in (USER_TURN_PROFILE, USER_FILE_PROFILE):
        with pytest.raises(ValueError):
            assemble_prompt(profile, capabilities=ASSISTANT_REPLY_CAPABILITIES)
    with pytest.raises(ValueError):
        assemble_prompt(ASSISTANT_REPLY_PROFILE, capabilities=USER_FILE_CAPABILITIES)
    current = assemble_prompt(ASSISTANT_REPLY_PROFILE, capabilities=ASSISTANT_REPLY_CAPABILITIES)
    assert len(current.system_message.encode()) == 3299
    assert current.record.prefix_sha256 == "bc7ec9b2f7ee9cb783ce21c51872e5a82e38841737b67caf37ac7c37ae50a983"
    assert current.record.profile == ASSISTANT_REPLY_PROFILE
    assert current.record.declared_capabilities == ASSISTANT_REPLY_CAPABILITIES
    assert "read_user_file" not in current.system_message and "assistant-body reader" not in current.system_message
