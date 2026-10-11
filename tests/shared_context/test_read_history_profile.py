"""One opt-in profile combines the existing past-user, user-file and past-reply readers without widening any of them."""

import pytest

from shared_context.behavior_assistant import _Session, key
from shared_context.behavior_assistant_reply import AssistantReplyRead, ReadAssistantReply, reply_turn
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
from shared_context.behavior_operations import _parse
from shared_context.behavior_text_files import ReadUserFile, UserFileRead, UserTextFile
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured, select, template
from test_instruction_flow import instruction_models, runtime_path, sdk_io
from test_instruction_text_files import post
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import open_turn

BODY = "HISTORY_FILE_CANARY\r\nExact original bytes: 42.\n"
REPLY = "HISTORY_REPLY_CANARY\r\n確切數值：42 公升。\n"


def history_turn(h, monkeypatch, *, profile=HISTORY_PROFILE, tools=HISTORY_CAPABILITIES):
    h, control, human = configured(h)
    selected, _ = select(h, value=template(profile, user_text_file_profile="fa-user-text-files-v1"))
    assert selected.status_code == 200, selected.text
    sdk_io(monkeypatch, expected_tools=tools)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": REPLY}])
    first = post(h, InstructionSubmit(request_id="history-original", content="Remember this file and tell me the volume.",
                                      files=(UserTextFile(name="input.txt", text=BODY),))).json()
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    return h, control, human, first


def stored_session(h, turn):
    row_key = key("session", turn.assignment.session_id)
    return _parse(h.store.read_snapshot((row_key,))[row_key].payload["data"], _Session)


def test_history_profile_reads_user_text_file_and_reply_of_one_past_turn(assistant, monkeypatch):
    h, control, human, first = history_turn(assistant, monkeypatch)
    turn = open_turn(h, control, human, "history-read-all", first["conversation_id"])
    try:
        user = turn.client.read_user_turn(1)
        assert user.text == "Remember this file and tell me the volume."
        file = turn.client.read_user_file(1, "input.txt")
        assert file.text == BODY and file.turn_source == user.source
        reply = turn.client.read_assistant_reply(1)
        assert reply.reply.text == REPLY and reply.reply.role == "assistant"
        assert reply_turn(user.source) == reply.turn
        assert stored_session(h, turn).user_turn_sources == (user.source,)
        before = h.store._guard()
        assert turn.client.read_user_file(1, "input.txt") == file
        assert turn.client.read_assistant_reply(1) == reply
        assert h.store._guard() == before
    finally:
        turn.client.close()


def test_history_profile_requires_explicit_literal_file_input(assistant):
    h, _, _ = configured(assistant)
    before = h.store._guard()
    selected, _ = select(h, value=template(HISTORY_PROFILE))
    assert selected.status_code == 422
    assert h.store._guard() == before


def test_revoked_scope_blocks_every_history_reader(assistant, monkeypatch):
    h, control, human, first = history_turn(assistant, monkeypatch)
    turn = open_turn(h, control, human, "history-revoked", first["conversation_id"])
    try:
        turn.client.read_user_file(1, "input.txt")
        control.revoke(human, "revoke-history-reader")
        before = h.store._guard()
        for read in (lambda: turn.client.read_user_turn(1), lambda: turn.client.read_user_file(1, "input.txt"),
                     lambda: turn.client.read_assistant_reply(1)):
            with pytest.raises(Problem):
                read()
        assert h.store._guard() == before
    finally:
        turn.client.close()


@pytest.mark.parametrize("profile,tools,route,command,model,code", [
    ("fa-instruction-user-files-v1",
     ("read_context", "execute_operation", "read_user_turn", "read_user_file"),
     "read-assistant-reply", ReadAssistantReply(request_id="read-assistant-reply", turn_number=1),
     AssistantReplyRead, "ASSISTANT_REPLY_READ_DISABLED"),
    ("fa-instruction-assistant-replies-v1",
     ("read_context", "execute_operation", "read_user_turn", "read_assistant_reply"),
     "read-user-file", ReadUserFile(request_id="read-user-file", turn_number=1, name="input.txt"),
     UserFileRead, "USER_FILE_READ_DISABLED"),
])
def test_single_reader_profiles_do_not_gain_the_other_reader(assistant, monkeypatch, profile, tools, route, command, model, code):
    h, control, human, first = history_turn(assistant, monkeypatch, profile=profile, tools=tools)
    turn = open_turn(h, control, human, "history-single-reader", first["conversation_id"])
    try:
        before = h.store._guard()
        with pytest.raises(Problem) as denied:
            turn.client._post(route, command, model)
        assert denied.value.status == 403 and denied.value.body["error"]["code"] == code
        assert h.store._guard() == before
        assert stored_session(h, turn).user_turn_sources == ()
    finally:
        turn.client.close()


def test_history_prompt_is_separate_and_older_reader_prompts_keep_their_bytes():
    import hashlib
    from shared_context.behavior_assistant_reply_prompt import ASSISTANT_REPLY_CAPABILITIES, ASSISTANT_REPLY_PROFILE
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.behavior_user_file_prompt import USER_FILE_CAPABILITIES, USER_FILE_PROFILE
    from shared_context.behavior_user_turn_prompt import USER_TURN_PROFILE
    for profile, size, sha in (
        (USER_TURN_PROFILE, 2822, "a79684eab440d77a92c2b3e19f3753718a486fbda4c4889c64029a23014c1372"),
        (USER_FILE_PROFILE, 3201, "9c8fd82691f5b40e5b01c5d90d15a807dec08828bdb9b2b3fb9ddf1f77965296"),
        (ASSISTANT_REPLY_PROFILE, 3299, "bc7ec9b2f7ee9cb783ce21c51872e5a82e38841737b67caf37ac7c37ae50a983"),
    ):
        old = assemble_prompt(profile).system_message.encode()
        assert len(old) == size and hashlib.sha256(old).hexdigest() == sha
        with pytest.raises(ValueError):
            assemble_prompt(profile, capabilities=HISTORY_CAPABILITIES)
    for subset in (USER_FILE_CAPABILITIES, ASSISTANT_REPLY_CAPABILITIES):
        with pytest.raises(ValueError):
            assemble_prompt(HISTORY_PROFILE, capabilities=subset)
    current = assemble_prompt(HISTORY_PROFILE, capabilities=HISTORY_CAPABILITIES)
    assert len(current.system_message.encode()) == 3678
    assert current.record.prefix_sha256 == "3d1c1e75d3eb95e36090c169195264cacee24d957e3e0e356b76f25db0d5648f"
    assert current.record.profile == HISTORY_PROFILE and current.record.declared_capabilities == HISTORY_CAPABILITIES
