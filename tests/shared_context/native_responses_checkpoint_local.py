"""Writes ``fixtures/native-responses-runtime-1.0.83``: one real native Responses turn saved by an older release.

Run from a tree of the older build (its own ``src`` and ``tests``), with that build's SDK installed and its runtime:
    <venv>/Scripts/python.exe -X utf8 native_responses_checkpoint_local.py <empty out dir> <copilot-runtime.exe>
The turn goes through the public Invocations host on the native turns path (``openai-responses``, auto checkpoint
profile, as stage selects for a new conversation); the model is an offline double. ``<out>/state`` is the local
State store, the same records the Foundry State store keeps on Hosted.
"""
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

QUESTION = "MARK-QUESTION-1: the shipment code is AZ-7731; remember it."
ANSWER = "MARK-ANSWER-1: noted, AZ-7731."
URL = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"


def main(out, runtime):
    out.mkdir(parents=True, exist_ok=False)
    work = Path(tempfile.mkdtemp(prefix="nx-native-ckpt-")).resolve()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        (work / key.lower()).mkdir()
        os.environ[key] = str(work / key.lower())
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "FOUNDRY_HOSTING_ENVIRONMENT", "FA_N0_STATE_PROBE", "FA_NATIVE_TURNS"):
        os.environ.pop(key, None)
    os.environ.update(COPILOT_SKIP_CLI_DOWNLOAD="1", AGENTSERVER_TASKS_BACKEND="local",
                      AGENTSERVER_STATE_ROOT="\\\\?\\" + str(work / "state") if os.name == "nt" else str(work / "state"))
    from test_native_turns import IDENTITY
    os.environ.update(FOUNDRY_AGENT_SESSION_ID=IDENTITY.session_id, FOUNDRY_AGENT_NAME=IDENTITY.agent_name)

    import httpx
    from starlette.testclient import TestClient
    import shared_context
    from shared_context.behavior_hosted import create_host
    from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
    from shared_context.native_checkpoint import conversation_store_name, open_durable_store
    from shared_context.native_sdk import NativeSdkExecutor
    from shared_context.native_turns import NativeTurnRuntime
    from test_instruction_responses_native import response_bytes
    from test_native_turn_wire import capture, submit

    requests = []

    def provider(request):
        assert str(request.url) == URL + "/responses"
        requests.append(json.loads(request.content))
        assert len(requests) == 1, "one model exchange is planned"
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              content=response_bytes(1, text=ANSWER))

    async def token():
        return "OFFLINE-native-MI"

    stops = []

    class ObserveStop(NativeSdkExecutor):
        async def __call__(self, *args):
            outcome = await super().__call__(*args)
            stops.append(outcome.sdk_stop)
            return outcome

    values = capture().model_dump(mode="json")
    platform = {"win32": "win32-x64", "linux": "linux-x64"}[sys.platform]
    values["template"]["settings"]["checkpoint_profile"] = {
        "name": "auto-no-custom-v1", "platform": platform, "provider": "openai-responses"}
    selected = NativeTurnCapture.model_validate_json(json.dumps(values))
    executor = ObserveStop(root=work / "run", runtime_path=runtime, api_url="https://api.offline.invalid",
                           model_url=URL, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime_ = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime_
    # The API labels the body with the profile it sends (behavior_native_responses_api.turn_profile).
    request = NativeSubmit(command=submit(text=QUESTION).command, capture=selected, state_profile=runtime_.state_profile)

    async def finished():
        while (value := await runtime_.read("rid-1")).status not in {"completed", "unknown", "busy_refused"}:
            await asyncio.sleep(0.02)
        return value

    async def bounded():
        return await asyncio.wait_for(finished(), timeout=120)

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime_)) as client:
        accepted = client.post("/invocations", json=request.model_dump(mode="json"), headers={
            "x-client-fa-state-profile": runtime_.state_profile, "x-agent-session-id": IDENTITY.session_id,
            "x-agent-invocation-id": "OFFLINE-platform-1", "x-agent-foundry-call-id": "OFFLINE-call-1"})
        assert accepted.status_code == 202, accepted.text
        receipt = client.portal.call(bounded)
        assert receipt.status == "completed", receipt.failure_code

        async def saved_ref():
            async with await open_durable_store(conversation_store_name(receipt.conversation_id), create=False) as store:
                return (await runtime_._turn(store, receipt)).checkpoints[0]

        ref = client.portal.call(saved_ref)
    shutil.copytree(work / "state", out / "state")
    provenance = {"shared_context": shared_context.__file__, "sdk": importlib.metadata.version("github-copilot-sdk"),
                  "runtime": stops[0].runtime_version, "state_profile": runtime_.state_profile,
                  "ref": [ref.sdk_version, ref.runtime_version, ref.codec]}
    print(json.dumps(provenance))
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
