"""Read exact literal files from the original completed user turn, never arbitrary paths."""

from types import MappingProxyType

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_assistant import _Session, key
from shared_context.behavior_operations import _parse
from shared_context.behavior_sdk_state import CheckpointProfile
from shared_context.behavior_text_files import ReadUserFile, UserFileRead, UserTextFile
from shared_context.behavior_user_file_prompt import USER_FILE_PROFILE
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_text_files import post
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn, ordinary, settle

BODY = "ORIGINAL_USER_FILE_CANARY\r\nExact original bytes: 42.\n"


def configured_reader(h, *, checkpoint=False, reader=True):
    if checkpoint:
        h.runtime.profiles = MappingProxyType({key: value.model_copy(update={"checkpoint_profile": CheckpointProfile()})
                                              for key, value in h.runtime.profiles.items()})
    h, control, human = configured(h)
    selected, _ = select(h, value=template(USER_FILE_PROFILE if reader else "fa-instruction-v1",
        checkpoint_profile=CheckpointProfile() if checkpoint else None,
        user_text_file_profile="fa-user-text-files-v1"))
    assert selected.status_code == 200, selected.text
    return h, control, human


def saved_file(h):
    submitted = post(h, InstructionSubmit(request_id="original-user-file", content="Save this original input.",
                                         files=(UserTextFile(name="input.txt", text=BODY),)))
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert h.runtime.controller.step(result["binding_id"]).state == "succeeded"
    return result


def stored_session(h, turn):
    row_key = key("session", turn.assignment.session_id)
    return _parse(h.store.read_snapshot((row_key,))[row_key].payload["data"], _Session)


def test_read_user_file_requires_explicit_reader_and_uses_original_registered_input(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    sdk_io(monkeypatch, expected_tools=("read_context", "execute_operation", "read_user_turn", "read_user_file"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Saved."}])
    first = saved_file(h)
    turn = open_turn(h, control, human, "read-original-file", first["conversation_id"])
    try:
        result = turn.client.read_user_file(1, "input.txt")
        assert result.text == BODY and result.file.name == "input.txt"
        assert result.turn_source.request_id == "original-user-file"
        assert result.turn_source.conversation_id == first["conversation_id"]
        assert result.usage == "historical-data-not-current-instructions"
        before = h.store._guard()
        assert turn.client.read_user_file(1, "input.txt") == result
        assert h.store._guard() == before
        current = stored_session(h, turn)
        assert current.user_turn_sources == (result.turn_source,)
    finally:
        turn.client.close()


@pytest.mark.parametrize("reader,turn_number,name", [
    (False, 1, "input.txt"), (True, 0, "input.txt"), (True, 2, "input.txt"),
    (True, 1, "not-recorded.txt"), (True, 1, "../input.txt"),
])
def test_missing_future_or_unapproved_file_read_does_not_add_source_or_effect(assistant, monkeypatch, reader, turn_number, name):
    h, control, human = configured_reader(assistant, reader=reader)
    tools = ("read_context", "execute_operation", *(("read_user_turn", "read_user_file") if reader else ()))
    sdk_io(monkeypatch, expected_tools=tools)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Saved."}])
    first = saved_file(h)
    turn = open_turn(h, control, human, "reject-file-read", first["conversation_id"])
    try:
        before = h.store._guard()
        with pytest.raises((Problem, ValueError)):
            turn.client.read_user_file(turn_number, name)
        assert h.store._guard() == before
        current = stored_session(h, turn)
        assert current.user_turn_sources == ()
    finally:
        turn.client.close()


def test_file_reader_setting_requires_explicit_literal_input(assistant):
    h, _, _ = configured(assistant)
    before = h.store._guard()
    selected, _ = select(h, value=template(USER_FILE_PROFILE))
    assert selected.status_code == 422
    assert h.store._guard() == before


def test_file_source_is_preserved_and_rechecked_across_followups(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    sdk_io(monkeypatch, expected_tools=("read_context", "execute_operation", "read_user_turn", "read_user_file"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Saved."}])
    first = saved_file(h)
    second = open_turn(h, control, human, "read-and-save-file-source", first["conversation_id"])
    try:
        read = second.client.read_user_file(1, "input.txt")
        settle(second, ordinary(second.assignment.sdk_session_id))
        third = open_turn(h, control, human, "after-file-source-save", first["conversation_id"])
        try:
            third.client.restore()
            assert stored_session(h, third).user_turn_sources == (read.turn_source,)
            assert third.client.read_user_file(1, "input.txt") == read
        finally:
            third.client.close()
    finally:
        second.client.close()


def test_revoked_scope_blocks_reusing_a_previously_read_file_source(assistant, monkeypatch):
    h, control, human = configured_reader(assistant)
    sdk_io(monkeypatch, expected_tools=("read_context", "execute_operation", "read_user_turn", "read_user_file"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Saved."}])
    first = saved_file(h)
    turn = open_turn(h, control, human, "current-file-read", first["conversation_id"])
    try:
        turn.client.read_user_file(1, "input.txt")
        control.revoke(human, "revoke-file-reader")
        before = h.store._guard()
        with pytest.raises(Problem):
            turn.client.read_user_file(1, "input.txt")
        assert h.store._guard() == before
    finally:
        turn.client.close()


@pytest.mark.parametrize("reader,number", [(False, 1), (True, 2)])
def test_server_rejects_forged_file_capability_or_nonpast_turn(assistant, monkeypatch, reader, number):
    h, control, human = configured_reader(assistant, reader=reader)
    tools = ("read_context", "execute_operation", *(("read_user_turn", "read_user_file") if reader else ()))
    sdk_io(monkeypatch, expected_tools=tools)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Saved."}])
    first = saved_file(h)
    turn = open_turn(h, control, human, "server-file-denial", first["conversation_id"])
    try:
        before = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client._post("read-user-file", ReadUserFile(request_id="read-user-file",
                turn_number=number, name="input.txt"), UserFileRead)
        assert denied.value.status == 403
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_production_provider_forwards_only_explicit_user_file_capability():
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
            "tools": [{"type": "function", "function": {"name": "read_user_file", "parameters": {"type": "object"}}}]}
    async def current():
        gates.append(True)
    async def run():
        try:
            with pytest.raises(Problem):
                await provider.send(body, text_reply=True, model="gpt-4.1-mini",
                                    user_turn_access=True, before_send=current)
            assert not calls
            response = await provider.send(body, text_reply=True, model="gpt-4.1-mini",
                user_turn_access=True, user_file_access=True, before_send=current)
            assert response.status_code == 200
            assert len(calls) == len(gates) == 1
        finally:
            await provider.aclose()
    asyncio.run(run())


def test_user_file_prompt_is_separate_and_cannot_expand_the_old_user_turn_catalog():
    import hashlib
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.behavior_user_file_prompt import USER_FILE_CAPABILITIES
    from shared_context.behavior_user_turn_prompt import USER_TURN_PROFILE
    old = assemble_prompt(USER_TURN_PROFILE)
    assert len(old.system_message.encode()) == 2822
    assert hashlib.sha256(old.system_message.encode()).hexdigest() == "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372"
    with pytest.raises(ValueError):
        assemble_prompt(USER_TURN_PROFILE, capabilities=USER_FILE_CAPABILITIES)
    current = assemble_prompt(USER_FILE_PROFILE, capabilities=USER_FILE_CAPABILITIES)
    assert len(current.system_message.encode()) == 3201
    assert current.record.prefix_sha256 == "9c8fd82691f5b40e5b01c5d90d15a807dec08828bdb9b2b3fb9ddf1f77965296"
    assert current.record.profile == USER_FILE_PROFILE
    assert current.record.declared_capabilities == USER_FILE_CAPABILITIES
    assert current.record.prefix_sha256 == hashlib.sha256(current.system_message.encode()).hexdigest()


def test_new_reader_profile_retains_the_literal_ui_captured_capability(assistant, monkeypatch):
    from shared_context.behavior_text_files import UserTextFileCapability
    h, _, _ = configured_reader(assistant)
    sdk_io(monkeypatch, expected_tools=("read_context", "execute_operation", "read_user_turn", "read_user_file"))
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Saved."}])
    first = saved_file(h)
    before = h.store._guard()
    response = h.client.get("/work-policy/v1/conversations/" + first["conversation_id"] + "/user-text-file-capability",
                            headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    capability = UserTextFileCapability.model_validate_json(response.content)
    assert capability.conversation_id == first["conversation_id"]
    assert capability.user_text_file_profile == "fa-user-text-files-v1" and capability.authorization is False
    assert h.store._guard() == before
