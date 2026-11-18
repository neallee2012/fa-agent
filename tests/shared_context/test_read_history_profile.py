"""One opt-in profile combines the existing past-user, user-file and past-reply readers without widening any of them."""

import pytest

from shared_context.behavior_assistant import _Session, key
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
from shared_context.behavior_operations import _parse
from shared_context.behavior_text_files import UserTextFile
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path, sdk_io
from test_instruction_text_files import post
from test_m3_auth import jwk_client, signing_key


def stored_session(h, turn):
    row_key = key("session", turn.assignment.session_id)
    return _parse(h.store.read_snapshot((row_key,))[row_key].payload["data"], _Session)


def test_history_profile_requires_explicit_literal_file_input(assistant_api):
    h, _, _ = configured(assistant_api)
    before = h.store._guard()
    selected, _ = select(h, value=template(HISTORY_PROFILE))
    assert selected.status_code == 422
    assert h.store._guard() == before


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
