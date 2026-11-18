"""Read the assistant's own exact saved reply from an original completed turn, never tool results or search."""

from types import MappingProxyType

import pytest

from shared_context.behavior_assistant import _Session, key
from shared_context.behavior_assistant_reply_prompt import ASSISTANT_REPLY_CAPABILITIES, ASSISTANT_REPLY_PROFILE
from shared_context.behavior_operations import _parse
from shared_context.behavior_sdk_state import CheckpointProfile
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key

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


def stored_session(h, turn):
    row_key = key("session", turn.assignment.session_id)
    return _parse(h.store.read_snapshot((row_key,))[row_key].payload["data"], _Session)


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
