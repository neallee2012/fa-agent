"""Fixed SDK/runtime with scripted model I/O; no live provider or Hosted deployment."""
import asyncio
import json

import httpx
import pytest
from copilot import CopilotClient

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.m2_copilot import run_copilot
from sdk_checkpoint_local import sse
from test_m2_copilot import runtime_path, workspace


@pytest.mark.parametrize("legacy_workspace", [False, True], ids=["virtual", "legacy-native"])
def test_fixed_sdk_resumes_without_original_host_work_directory(workspace, runtime_path, monkeypatch, legacy_workspace):
    async def scenario():
        sid = "sdk-workspace-legacy" if legacy_workspace else "sdk-workspace-virtual"
        previous = None
        calls = []
        for number in (1, 2):
            work = workspace / f"work-{number}"
            work.mkdir()
            fs = SdkSessionFiles(sid, previous)
            original_bytes = None if previous is None else previous.encoded()

            async def reserve(_):
                return None

            def respond(request):
                assert request.url.host == "offline.invalid"
                body = json.loads(request.content)
                if number == 2:
                    assert "First original SDK reply." in json.dumps(body["messages"])
                calls.append(number)
                return sse(request, {"content": "First original SDK reply." if number == 1 else "Original history restored."},
                           len(calls))

            with monkeypatch.context() as patch:
                if number == 1 and legacy_workspace:
                    create = CopilotClient.create_session

                    async def create_legacy(instance, **options):
                        options["working_directory"] = str(work)
                        return await create(instance, **options)

                    patch.setattr(CopilotClient, "create_session", create_legacy)
                outcome = await run_copilot(
                    state_dir=workspace / f"sdk-{number}", working_directory=work, sdk_session_id=sid,
                    resume=number == 2, prompt="Continue this controlled local SDK test.", tools=[],
                    api_url="https://offline.invalid", token_provider=lambda: "OFFLINE",
                    on_event=lambda _: None, runtime_path=runtime_path, timeout_seconds=30,
                    http_transport=httpx.MockTransport(respond), instruction=True, session_files=fs,
                    behavior_boundary=AssistantModelBoundary(reserve, text_reply=True),
                    system_message="Controlled SDK workspace compatibility test.",
                )
            assert outcome["sdk_stop"]["graceful"] is True
            assert not fs.failed
            if original_bytes is not None:
                assert fs.previous.encoded() == original_bytes
            previous = fs.snapshot()
            if number == 1:
                work.rename(workspace / "retained-original-work")
            else:
                assert outcome["final_message"] == "Original history restored."
        assert calls == [1, 2]

    asyncio.run(scenario())
