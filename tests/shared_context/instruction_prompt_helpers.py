"""從 test_instruction_prompt.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import json
from types import MappingProxyType, SimpleNamespace
from shared_context import m2_copilot
from shared_context.behavior_assistant import AssistantProfile, key
from shared_context.behavior_instruction import TurnArchive
from shared_context.behavior_instruction_prompt import INSTRUCTION_CAPABILITIES
from shared_context.behavior_work_policy_api import install_work_policy_routes
from instruction_flow_helpers import sdk_io
from work_policy_lifecycle_helpers import enabled


def configured(assistant, profile="fa-instruction-v1"):
    assistant.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate(value.model_copy(update={"prompt_profile": profile}).model_dump())
        for name, value in assistant.runtime.profiles.items()
    })
    h, control, human, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    return h, control, human


def sdk_options(monkeypatch, *, expected_tools=INSTRUCTION_CAPABILITIES):
    calls = sdk_io(monkeypatch, expected_tools=expected_tools)
    factory = m2_copilot.CopilotClient.side_effect
    configurations = []

    def capture(**connection):
        sdk = factory(**connection)
        opened = sdk.create_session.side_effect

        async def observe(*args, **options):
            configurations.append(options)
            return await opened(*args, **options)

        sdk.create_session.side_effect = observe
        sdk.resume_session.side_effect = observe
        return sdk

    m2_copilot.CopilotClient.side_effect = capture
    return calls, configurations


def saved_turn(h, job_id):
    archive_key = key("text-archive", job_id)
    row = h.store.read_snapshot((archive_key,))[archive_key]
    return TurnArchive.model_validate_json(json.dumps(row.payload["data"]))
