"""One real pinned Copilot runtime turn through the public N1 host; model/MI transport is offline."""

import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NativeResult, NativeSubmit, NativeTurnCapture
from shared_context.native_checkpoint import open_durable_store, verify_checkpoint, conversation_store_name
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from test_m2_copilot import runtime_path
from test_native_turn_wire import agent_receive, capture, submit
from test_native_turns import IDENTITY


@pytest.mark.parametrize("agent_input", [False, True])
def test_pinned_sdk_executes_one_native_completions_turn_and_saves_original_result(
        monkeypatch, runtime_path, agent_input):
    from copilot.session import CopilotSession
    from shared_context.behavior_text_files import UserTextFile

    sdk_inputs = []
    original_send = CopilotSession.send

    async def observe_input(session, prompt, **options):
        sdk_inputs.append((prompt, options.get("attachments")))
        return await original_send(session, prompt, **options)

    monkeypatch.setattr(CopilotSession, "send", observe_input)
    root_value = os.environ.get("N1_NATIVE_WORKSPACE")
    if not root_value:
        pytest.fail("Set a session-private N1_NATIVE_WORKSPACE; never use the real user HOME.")
    root = Path(root_value).resolve() / ("agent" if agent_input else "human")
    root.mkdir(parents=True, exist_ok=False)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        isolated = root / key.lower()
        isolated.mkdir()
        monkeypatch.setenv(key, str(isolated))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                "FOUNDRY_HOSTING_ENVIRONMENT", "FA_N0_STATE_PROBE", "FA_NATIVE_TURNS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(root / "state"))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", IDENTITY.agent_name)
    import _socket

    def deny_external(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The N1 native case may not open external network connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    requests, stops, input_checks = [], [], []
    final_text = "OFFLINE-native-complete"
    url = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"

    def provider(request):
        assert str(request.url) == url + "/chat/completions"
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer OFFLINE-native-MI"
        assert "OFFLINE-native-MI" not in request.content.decode()
        assert payload["model"] == "OFFLINE-model" and payload["stream"] is True
        assert payload["stream_options"]["include_usage"] is True
        if agent_input:
            serialized = json.dumps(payload, ensure_ascii=False)
            input_checks.append({
                "source": "Message from another Agent." in serialized,
                "data_only": "not a new human instruction" in serialized,
                "full_file": "A" * 7_600 in serialized,
                "file_chars": serialized.count("A"),
            })
            assert input_checks[-1]["source"] and input_checks[-1]["data_only"]
            assert input_checks[-1]["file_chars"] >= 500
        requests.append(payload)
        assert len(requests) <= (1 if agent_input else 2), "Only the planned offline model exchanges are allowed"
        common = {"id": "offline-native-answer", "object": "chat.completion.chunk", "created": 1, "model": "OFFLINE-model"}
        events = [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": final_text},
                                    "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**common, "choices": [], "usage": {"prompt_tokens": 17, "completion_tokens": 7, "total_tokens": 24}},
        ]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())

    async def token():
        return "OFFLINE-native-MI"

    class ObserveStop(NativeSdkExecutor):
        async def __call__(self, *args):
            outcome = await super().__call__(*args)
            stops.append(outcome.sdk_stop)
            return outcome

    selected = capture().model_dump(mode="json")
    selected["template"]["settings"]["model_provider"] = None
    selected = NativeTurnCapture.model_validate_json(json.dumps(selected))
    executor = ObserveStop(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                           model_url=url, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    request = NativeSubmit(command=submit(text="Reply exactly OFFLINE-native-complete.").command, capture=selected)
    if agent_input:
        request = agent_receive(rid="rid-1").model_copy(update={"capture": selected})
        request = request.model_copy(update={"source": request.source.model_copy(update={"hop": 12})})
        command = request.command.model_copy(update={
            "content": "Original agent brief " + "B" * 230,
            "files": (UserTextFile(name="original.txt", text="A" * 7_600),),
        })
        request = type(request).model_validate_json(request.model_copy(update={"command": command}).model_dump_json())

    async def finished():
        while True:
            receipt = await runtime.read("rid-1")
            if receipt.status in {"completed", "unknown", "busy_refused"}:
                return receipt
            await asyncio.sleep(0.02)

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        accepted = client.post("/invocations", json=request.model_dump(mode="json"), headers={
            "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
            "x-agent-invocation-id": "OFFLINE-native-platform", "x-agent-foundry-call-id": "OFFLINE-native-call",
        })
        assert accepted.status_code == 202, accepted.text

        async def bounded_observation():
            return await asyncio.wait_for(finished(), timeout=60)

        receipt = client.portal.call(bounded_observation)
        assert receipt.status == "completed", (receipt.failure_code, input_checks)
        result = client.portal.call(runtime.result, NativeResult(
            conversation_id=receipt.conversation_id, request_id="rid-1"))
        assert result.text == final_text and result.complete
        assert receipt.usage.usage[0].wire_api == "completions"
        assert receipt.usage.usage[0].input_tokens == 17
        if agent_input:
            assert sdk_inputs[0][1][0]["text"] == request.command.files[0].text
            assert receipt.command.files == request.command.files
            assert request.command.content in sdk_inputs[0][0]

        async def stored_checkpoint():
            meta, _ = await runtime._meta(receipt.conversation_id)
            async with await open_durable_store(conversation_store_name(receipt.conversation_id), create=False) as store:
                turn = await runtime._turn(store, receipt)
                restored = await verify_checkpoint(store, runtime._scope(meta), turn.checkpoints[0])
                assert final_text in restored.files["events.jsonl"]
                assert "OFFLINE-native-MI" not in restored.encoded().decode()

        client.portal.call(stored_checkpoint)
        if not agent_input:
            # B1 PA-07: a second conversation starts from its own state; the runtime stamps its local time.
            other = NativeSubmit(command=submit(rid="rid-2", text="Second conversation question.").command,
                                 capture=selected)
            assert client.post("/invocations", json=other.model_dump(mode="json"), headers={
                "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
                "x-agent-invocation-id": "OFFLINE-native-platform-2", "x-agent-foundry-call-id": "OFFLINE-native-call-2",
            }).status_code == 202

            async def second():
                while (value := await runtime.read("rid-2")).status not in {"completed", "unknown", "busy_refused"}:
                    await asyncio.sleep(0.02)
                return value

            async def bounded_second():
                return await asyncio.wait_for(second(), timeout=60)

            assert client.portal.call(bounded_second).conversation_id != receipt.conversation_id
            users = [[m for m in body["messages"] if m["role"] == "user"] for body in requests]
            texts = [json.dumps(messages, ensure_ascii=False) for messages in users]
            assert "Reply exactly OFFLINE-native-complete." in texts[0] and "<current_datetime>" in texts[0]
            assert "Second conversation question." in texts[1] and "<current_datetime>" in texts[1]
            assert "Reply exactly OFFLINE-native-complete." not in json.dumps(requests[1], ensure_ascii=False)
            assert final_text not in json.dumps(requests[1], ensure_ascii=False)
            requests.pop()
    assert len(requests) == len(stops if agent_input else stops[:1]) == 1
    # ADR-0005: an idle native turn keeps its SDK; the checkpoint above was saved from the live runtime's files.
    assert stops[0].runtime_version == "1.0.83" and stops[0].release == "fa-resident-worker-poc-v1"
    assert stops[0].exit_code is None
    proof = {
        "sdk": importlib.metadata.version("github-copilot-sdk"), "runtime": stops[0].runtime_version,
        "sdk_stop": stops[0].model_dump(mode="json"), "status": receipt.status, "model_requests": len(requests),
        "result_digest": receipt.result.result_digest, "checkpoint_sha256": receipt.result.checkpoint_sha256,
        "usage": receipt.usage.model_dump(mode="json"), "cloud_model_or_storage": False,
    }
    (root / "proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")


def test_pinned_sdk_resume_replaces_the_previous_role_in_the_same_conversation(monkeypatch, runtime_path):
    """B1 PA-05: a newly approved role reaches the next turn of the same conversation alone, never stacked."""
    import hashlib
    from shared_context.behavior_agent_role_prompt import RolePromptCapture

    root_value = os.environ.get("N1_NATIVE_WORKSPACE")
    if not root_value:
        pytest.fail("Set a session-private N1_NATIVE_WORKSPACE; never use the real user HOME.")
    root = Path(root_value).resolve() / "replace"
    root.mkdir(parents=True, exist_ok=False)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        isolated = root / key.lower()
        isolated.mkdir()
        monkeypatch.setenv(key, str(isolated))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                "FOUNDRY_HOSTING_ENVIRONMENT", "FA_N0_STATE_PROBE", "FA_NATIVE_TURNS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(root / "state"))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", IDENTITY.agent_name)
    import _socket

    def deny_external(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The N1 native case may not open external network connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    url = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "Only one model exchange per turn is planned"
        common = {"id": f"offline-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
                  "model": "OFFLINE-model"}
        events = [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"REPLY-{len(requests)}"},
                                    "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**common, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}},
        ]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())

    async def token():
        return "OFFLINE-native-MI"

    def selection(version, text, origin):
        values = capture().model_dump(mode="json")
        values["template"]["settings"]["model_provider"] = None
        if origin:
            values["template"]["settings"]["turn_origin_profile"] = "fa-instruction-context-origin-v1"
        values["role_prompt"] = RolePromptCapture(
            version=version, sha256=hashlib.sha256(text.encode()).hexdigest(), text=text).model_dump(mode="json")
        return NativeTurnCapture.model_validate_json(json.dumps(values))

    executor = NativeSdkExecutor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=url, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime

    async def finished(rid):
        while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
            await asyncio.sleep(0.02)
        return value

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        def post(rid, cid, text, chosen):
            request = NativeSubmit(command=submit(rid=rid, cid=cid, text=text).command, capture=chosen)
            return client.post("/invocations", json=request.model_dump(mode="json"), headers={
                "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
                "x-agent-invocation-id": "OFFLINE-" + rid, "x-agent-foundry-call-id": "OFFLINE-call-" + rid,
            })

        def turn(rid, cid, text, chosen):
            assert post(rid, cid, text, chosen).status_code == 202

            async def bounded():
                return await asyncio.wait_for(finished(rid), timeout=60)

            receipt = client.portal.call(bounded)
            assert receipt.status == "completed", receipt.failure_code
            return receipt

        first = turn("rid-1", None, "First question.", selection(1, "ROLE-ALPHA keeps answers short.", False))
        second = turn("rid-2", first.conversation_id, "Second question.",
                      selection(2, "ROLE-BRAVO explains each step.", False))
        # The conversation's template stays the one it started with; only the role head and capabilities are per-turn.
        changed = post("rid-3", first.conversation_id, "Third question.", selection(2, "ROLE-BRAVO explains each step.", True))
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "NATIVE_CAPTURE_CHANGED"
    assert second.conversation_id == first.conversation_id and second.turn_number == first.turn_number + 1
    systems = [[m for m in body["messages"] if m["role"] == "system"] for body in requests]
    assert [len(found) for found in systems] == [1, 1]
    before, after = (json.dumps(found, ensure_ascii=False) for found in systems)
    assert "ROLE-ALPHA" in before and "ROLE-BRAVO" not in before
    assert "ROLE-BRAVO" in after and after.count("## Team-approved agent role") == 1
    # Replaced, not stacked: the old role text is gone from the whole request, while the history is resumed.
    later = json.dumps(requests[1], ensure_ascii=False)
    assert "ROLE-ALPHA" not in later
    assert "First question." in later and "REPLY-1" in later and "Second question." in later


@pytest.mark.parametrize("profile", ["fa-instruction-agent-role-v2", "fa-instruction-agent-role-v3",
                                     "fa-instruction-agent-role-v4"])
def test_pinned_sdk_sends_agent_role_v2_with_native_tools_and_the_role_text_last(monkeypatch, runtime_path, profile):
    """B1 SP series: the shared native prompt, the original-message readers, no execute_operation; role optional."""
    import hashlib
    from shared_context.behavior_agent_role_prompt import RolePromptCapture, _SHARED_ROLE_PROMPTS
    from shared_context.behavior_skill_prompt import assemble_prompt
    AGENT_ROLE_V2_PROFILE, ROLE_V2_PROMPT = profile, _SHARED_ROLE_PROMPTS[profile]

    root_value = os.environ.get("N1_NATIVE_WORKSPACE")
    if not root_value:
        pytest.fail("Set a session-private N1_NATIVE_WORKSPACE; never use the real user HOME.")
    root = Path(root_value).resolve() / ("role-" + profile[-2:])
    root.mkdir(parents=True, exist_ok=False)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        isolated = root / key.lower()
        isolated.mkdir()
        monkeypatch.setenv(key, str(isolated))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                "FOUNDRY_HOSTING_ENVIRONMENT", "FA_N0_STATE_PROBE", "FA_NATIVE_TURNS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(root / "state"))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", IDENTITY.agent_name)
    import _socket

    def deny_external(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The N1 native case may not open external network connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    url = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"
    requests = []

    def provider(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "One model exchange per turn"
        common = {"id": f"offline-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
                  "model": "OFFLINE-model"}
        events = [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"REPLY-{len(requests)}"},
                                    "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**common, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}},
        ]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())

    async def token():
        return "OFFLINE-native-MI"

    record = assemble_prompt(AGENT_ROLE_V2_PROFILE).record

    def selection(text):
        values = capture().model_dump(mode="json")
        values["template"]["role"] = {"profile": AGENT_ROLE_V2_PROFILE, "prefix_sha256": record.prefix_sha256,
                                      "capability_catalog_version": record.capability_catalog_version}
        values["template"]["settings"].update(model_provider=None,
                                              turn_origin_profile="fa-instruction-context-origin-v1")
        values["role_prompt"] = None if text is None else RolePromptCapture(
            version=1, sha256=hashlib.sha256(text.encode()).hexdigest(), text=text).model_dump(mode="json")
        return NativeTurnCapture.model_validate_json(json.dumps(values))

    executor = NativeSdkExecutor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=url, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime

    async def finished(rid):
        while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
            await asyncio.sleep(0.02)
        return value

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        for rid, text in (("rid-role", "ROLE-ALPHA keeps answers short."), ("rid-plain", None)):
            request = NativeSubmit(command=submit(rid=rid, text="Hello.").command, capture=selection(text))
            assert client.post("/invocations", json=request.model_dump(mode="json"), headers={
                "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
                "x-agent-invocation-id": "OFFLINE-" + rid, "x-agent-foundry-call-id": "OFFLINE-call-" + rid,
            }).status_code == 202

            async def bounded(rid=rid):
                return await asyncio.wait_for(finished(rid), timeout=60)

            receipt = client.portal.call(bounded)
            assert receipt.status == "completed", receipt.failure_code
    systems = [[m["content"] for m in body["messages"] if m["role"] == "system"] for body in requests]
    tools = [{tool["function"]["name"] for tool in body.get("tools", [])} for body in requests]
    assert [len(items) for items in systems] == [1, 1]
    with_role, plain = systems[0][0], systems[1][0]
    assert with_role.startswith(ROLE_V2_PROMPT) and plain.startswith(ROLE_V2_PROMPT)
    assert "## Native turn origin" in with_role and "## Native turn origin" in plain
    # The Team's role text is the last part, after the origin section; without one there is no role section.
    assert with_role.count("## Team-approved agent role\n") == 1
    assert with_role.endswith("\n\nROLE-ALPHA keeps answers short.")
    assert with_role.index("## Native turn origin") < with_role.index("## Team-approved agent role")
    assert "## Team-approved agent role" not in plain and "ROLE-ALPHA" not in plain
    for names in tools:
        assert {"read_context", "read_user_turn", "read_user_file", "read_assistant_reply"} <= names
        assert "execute_operation" not in names



def test_pinned_sdk_carries_the_first_turns_static_profile_only_in_history(monkeypatch, runtime_path):
    """PA-14 S1: the static profile read reaches turn 1's model request; turn 2 keeps it only through resumed history."""
    from shared_context import behavior_private_memory as memory
    from shared_context.native_private_memory import NativePrivateMemory

    root_value = os.environ.get("N1_NATIVE_WORKSPACE")
    if not root_value:
        pytest.fail("Set a session-private N1_NATIVE_WORKSPACE; never use the real user HOME.")
    root = Path(root_value).resolve() / "memory-tiers"
    root.mkdir(parents=True, exist_ok=False)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        isolated = root / key.lower()
        isolated.mkdir()
        monkeypatch.setenv(key, str(isolated))
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_CERTIFICATE_PATH",
                "FOUNDRY_HOSTING_ENVIRONMENT", "FA_N0_STATE_PROBE", "FA_NATIVE_TURNS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("COPILOT_SKIP_CLI_DOWNLOAD", "1")
    monkeypatch.setenv("AGENTSERVER_TASKS_BACKEND", "local")
    monkeypatch.setenv("AGENTSERVER_STATE_ROOT", str(root / "state"))
    monkeypatch.setenv("FOUNDRY_AGENT_SESSION_ID", IDENTITY.session_id)
    monkeypatch.setenv("FOUNDRY_AGENT_NAME", IDENTITY.agent_name)
    import _socket

    def deny_external(sock, address):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("The N1 native case may not open external network connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    project = "https://offline-fixture.services.ai.azure.com/api/projects/offline"
    requests, searches = [], []

    def provider(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "Only one model exchange per turn is planned"
        common = {"id": f"offline-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
                  "model": "OFFLINE-model"}
        events = [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": f"REPLY-{len(requests)}"},
                                    "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**common, "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}},
        ]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())

    def memory_service(request):
        body = json.loads(request.content)
        searches.append(body)
        # The static read returns the profile; a message search returns only the episode.
        item = ({"memory_id": "p1", "kind": "user_profile", "content": "PROFILE-LIMA wants the conclusion first",
                 "updated_at": 1790812800} if "items" not in body else
                {"memory_id": "e1", "kind": "chat_summary", "content": "EPISODE-KILO chose Postgres",
                 "updated_at": 1790899200})
        return httpx.Response(200, json={"search_id": "s1", "memories": [{"memory_item": item}]})

    async def token():
        return "OFFLINE-native-MI"

    memories = NativePrivateMemory.from_environment(token, environ={
        memory.STORE_ENV: "fa-memory-store", memory.ENDPOINT_ENV: project, memory.AGENT_ENV: IDENTITY.agent_name},
        transport=httpx.MockTransport(memory_service))
    values = capture().model_dump(mode="json")
    values["template"]["settings"]["model_provider"] = None
    chosen = NativeTurnCapture.model_validate_json(json.dumps(values))
    executor = NativeSdkExecutor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=project + "/openai/v1", token_provider=token,
                                 model_transport=httpx.MockTransport(provider), private_memory=memories)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime

    async def finished(rid):
        while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
            await asyncio.sleep(0.02)
        return value

    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        def turn(rid, cid, text):
            request = NativeSubmit(command=submit(rid=rid, cid=cid, text=text).command, capture=chosen)
            assert client.post("/invocations", json=request.model_dump(mode="json"), headers={
                "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
                "x-agent-invocation-id": "OFFLINE-" + rid, "x-agent-foundry-call-id": "OFFLINE-call-" + rid,
            }).status_code == 202

            async def bounded():
                return await asyncio.wait_for(finished(rid), timeout=60)

            receipt = client.portal.call(bounded)
            assert receipt.status == "completed", receipt.failure_code
            return receipt

        first = turn("rid-1", None, "First question.")
        turn("rid-2", first.conversation_id, "Second question.")
    assert ["items" in body for body in searches].count(False) == 1 and len(searches) == 3
    profile = "[profile, 2026-10-01 00:00 UTC] PROFILE-LIMA wants the conclusion first"
    episode = "[episode, 2026-10-02 00:00 UTC] EPISODE-KILO chose Postgres"
    first_request, second_request = (json.dumps(body, ensure_ascii=False) for body in requests)
    assert first_request.count(profile) == 1 and first_request.count(episode) == 1
    # Turn 2 sends no second static read: the profile is present once, in turn 1's resumed user message.
    assert second_request.count("PROFILE-LIMA") == 1 and second_request.count(episode) == 2
    assert second_request.index("PROFILE-LIMA") < second_request.index("REPLY-1") < second_request.index("Second question.")
