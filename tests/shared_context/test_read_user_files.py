"""Read exact literal files from the original completed user turn, never arbitrary paths."""

from types import MappingProxyType

import pytest

from shared_context.behavior_assistant import _Session, key
from shared_context.behavior_operations import _parse
from shared_context.behavior_sdk_state import CheckpointProfile
from shared_context.behavior_user_file_prompt import USER_FILE_PROFILE
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key

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


def stored_session(h, turn):
    row_key = key("session", turn.assignment.session_id)
    return _parse(h.store.read_snapshot((row_key,))[row_key].payload["data"], _Session)


def test_file_reader_setting_requires_explicit_literal_input(assistant_api):
    h, _, _ = configured(assistant_api)
    before = h.store._guard()
    selected, _ = select(h, value=template(USER_FILE_PROFILE))
    assert selected.status_code == 422
    assert h.store._guard() == before


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
