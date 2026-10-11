"""B1 TC S1: the pinned runtime compacts automatically; a native conversation must keep going past it.

Real pinned SDK 1.0.13 / runtime 1.0.83 through the public N1 host; model transport is offline.
"""

import asyncio
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_instruction_models import InstructionModel
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_checkpoint import conversation_store_name, open_durable_store, verify_checkpoint
from shared_context.native_turns import NativeTurnRuntime
from test_m2_copilot import runtime_path
from test_native_turn_wire import capture, submit
from test_native_turns import IDENTITY, local_env

FILLER = "The quick brown fox jumps over the lazy dog near river bank number {i}. "
URL = "https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1"


def isolated(monkeypatch, name):
    root_value = os.environ.get("N1_NATIVE_WORKSPACE")
    if not root_value:
        pytest.fail("Set a session-private N1_NATIVE_WORKSPACE; never use the real user HOME.")
    root = Path(root_value).resolve() / name
    root.mkdir(parents=True, exist_ok=False)
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
        (root / key.lower()).mkdir()
        monkeypatch.setenv(key, str(root / key.lower()))
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
            raise AssertionError("The native case may not open external network connections")
        return _socket.socket.connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    return root


def small_capture(checkpoint_profile=None, prompt=5000, model_provider=None):
    values = capture().model_dump(mode="json")
    model = InstructionModel(id="OFFLINE-model", display_name="OFFLINE model", context_window_tokens=prompt + 1000,
                             max_prompt_tokens=prompt, max_output_tokens=512)
    values["model"] = model.model_dump(mode="json")
    values["model_capacity"] = model.capacity_snapshot().model_dump(mode="json")
    settings = values["template"]["settings"]
    settings.update(model_provider=model_provider, model_capacity_profile="fa-model-capacity-v1")
    if checkpoint_profile is not None:
        settings["checkpoint_profile"] = checkpoint_profile
    return NativeTurnCapture.model_validate_json(json.dumps(values))


def provider_double(requests, turn=lambda: None):
    def provider(request):
        body = json.loads(request.content)
        compaction = "Please create a detailed summary of the conversation so far" in json.dumps(body["messages"])
        requests.append({"compaction": compaction, "messages": len(body["messages"]), "bytes": len(request.content),
                         "text": json.dumps(body["messages"], ensure_ascii=False), "turn": turn()})
        reply = ("<overview>Offline summary.</overview>\n<history>1. The user sent MARK-TURN-1 and filler.</history>\n"
                 "<checkpoint_title>Offline filler summary</checkpoint_title>") if compaction else f"REPLY-{len(requests)}"
        return completion_stream(len(requests), {"role": "assistant", "content": reply}, "stop",
                                 prompt_tokens=len(request.content) // 4)
    return provider


def completion_stream(number, delta, finish_reason, *, prompt_tokens):
    """One offline Chat Completions SSE answer: the delta, its finish reason, then usage."""
    common = {"id": f"offline-{number}", "object": "chat.completion.chunk", "created": 1, "model": "OFFLINE-model"}
    events = [
        {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
        {**common, "choices": [], "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 9,
                                            "total_tokens": prompt_tokens + 9}},
    ]
    raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())


def run_turns(monkeypatch, runtime_path, name, selected, turns=7, paragraphs=60, provider=None, hitl_client=None):
    root = isolated(monkeypatch, name)
    requests, receipts, saved = [], [], []

    async def token():
        return "OFFLINE-native-MI"

    executor = NativeSdkExecutor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=URL, token_provider=token, hitl_client=hitl_client,
                                 model_transport=httpx.MockTransport(
                                     (provider or provider_double)(requests, lambda: len(receipts) + 1)))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        cid = None
        for n in range(1, turns + 1):
            rid = f"rid-{n}"
            text = ("MARK-TURN-1 " if n == 1 else "") + "".join(FILLER.format(i=n * 1000 + j) for j in range(paragraphs))
            request = NativeSubmit(command=submit(rid=rid, cid=cid, text=text).command, capture=selected)
            response = client.post("/invocations", json=request.model_dump(mode="json"), headers={
                "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
                "x-agent-invocation-id": "OFFLINE-" + rid, "x-agent-foundry-call-id": "OFFLINE-call-" + rid,
            })
            if response.status_code != 202:
                receipts.append(("refused", response.status_code, response.json()["error"]["code"]))
                break

            async def finished(rid=rid):
                async def poll():
                    while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
                        await asyncio.sleep(0.02)
                    return value
                return await asyncio.wait_for(poll(), timeout=120)

            receipt = client.portal.call(finished)
            receipts.append((receipt.status, receipt.failure_code, receipt.turn_number))
            cid = receipt.conversation_id
            if receipt.status != "completed":
                break

            async def checkpoint(receipt=receipt):
                meta, _ = await runtime._meta(receipt.conversation_id)
                async with await open_durable_store(conversation_store_name(receipt.conversation_id), create=False) as store:
                    turn = await runtime._turn(store, receipt)
                    ref = turn.checkpoints[0]
                    state = await verify_checkpoint(store, runtime._scope(meta), ref,
                                                    checkpoint_profile=receipt.capture.template.settings.checkpoint_profile)
                    return ref, state

            saved.append(client.portal.call(checkpoint))
        originals = {f"rid-{n}": client.portal.call(runtime.read, f"rid-{n}").command.content
                     for n, receipt in enumerate(receipts, 1) if receipt[0] != "refused"}
    return requests, receipts, saved, originals




PLATFORM = {"win32": "win32-x64", "linux": "linux-x64"}[__import__("sys").platform]
AUTO = {"name": "auto-no-custom-v1", "platform": PLATFORM, "provider": "openai-completions"}


def test_auto_profile_keeps_a_long_conversation_going_after_the_runtime_compacts(monkeypatch, runtime_path):
    requests, receipts, saved, originals = run_turns(
        monkeypatch, runtime_path, "auto", small_capture(AUTO, prompt=20000), turns=13, paragraphs=100)
    assert [status for status, _, _ in receipts] == ["completed"] * 13, receipts
    compacted = [request["turn"] for request in requests if request["compaction"]]
    assert compacted and compacted[0] < 13, "the declared 20000-token prompt limit must make the pinned runtime compact"
    # The next turn resumes from the saved state and carries the runtime's summary instead of the full history.
    following = [request["text"] for request in requests if request["turn"] == compacted[0] + 1 and not request["compaction"]]
    assert following and "Some of the conversation history has been summarized" in following[0]
    assert "Offline summary" in following[0]
    # TC-06: FA's fixed notice starts only with the first turn that resumes a compacted state.
    notice = "FA note: earlier parts of this conversation were summarized"
    assert notice in following[0]
    assert not any(notice in request["text"] for request in requests if request["turn"] <= compacted[0])
    # Saved as ordinary state with the runtime's own pairs; the private system prompt is still filtered out.
    ref, state = saved[-1]
    assert ref.codec == "native-session-fs-v1" and ref.source is None
    events = [json.loads(line) for line in state.files["events.jsonl"].splitlines() if line.strip()]
    kinds = [event["type"] for event in events]
    assert kinds.count("session.compaction_start") == kinds.count("session.compaction_complete") >= 1
    assert all(event["data"].get("content") in (None, "") for event in events if event["type"] == "system.message")
    assert not any(kind in {"model.model_call_success", "model.messages_snapshot"} for kind in kinds)
    # The originals stay exact in the RID receipts, independent of any summary.
    assert originals["rid-1"].startswith("MARK-TURN-1 ") and len(originals) == 13


def large_replies(requests, turn=lambda: None):
    """Each ordinary reply adds about 30k tokens, so the context grows past 128000 within a few turns."""
    large = "".join(FILLER.format(i=j) for j in range(1900))

    def provider(request):
        body = json.loads(request.content)
        compaction = "Please create a detailed summary of the conversation so far" in json.dumps(body["messages"])
        requests.append({"compaction": compaction, "turn": turn()})
        reply = ("<overview>Offline summary.</overview>\n<history>1. filler.</history>\n"
                 "<checkpoint_title>t</checkpoint_title>") if compaction else large
        common = {"id": f"offline-{len(requests)}", "object": "chat.completion.chunk", "created": 1, "model": body["model"]}
        events = [
            {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**common, "choices": [], "usage": {"prompt_tokens": len(request.content) // 4,
                                                "completion_tokens": len(reply) // 4,
                                                "total_tokens": (len(request.content) + len(reply)) // 4}},
        ]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())
    return provider


@pytest.mark.parametrize("declared,first", [(None, 5), (272000, 8)])
def test_the_runtime_compacts_at_the_declared_input_limit_even_above_its_128000_default(
        monkeypatch, runtime_path, declared, first):
    """TC-04: gpt-5.4-mini's documented 272000 input reaches the pinned runtime, which compacts at 80% of it."""
    values = capture().model_dump(mode="json")
    settings = values["template"]["settings"]
    settings.update(model_provider=None, checkpoint_profile=AUTO)
    model = InstructionModel(id="gpt-5.4-mini", display_name="gpt-5.4-mini", **({} if declared is None else {
        "context_window_tokens": 400000, "max_prompt_tokens": declared, "max_output_tokens": 128000}))
    values["model"] = model.model_dump(mode="json")
    if declared is not None:
        values["model_capacity"] = model.capacity_snapshot().model_dump(mode="json")
        settings["model_capacity_profile"] = "fa-model-capacity-v1"
    selected = NativeTurnCapture.model_validate_json(json.dumps(values))
    requests, receipts, _, _ = run_turns(monkeypatch, runtime_path, f"declared-{declared}", selected,
                                         turns=first, paragraphs=5, provider=large_replies)
    assert [status for status, _, _ in receipts] == ["completed"] * first, receipts
    # Without a declaration the runtime's 128000 default compacts at turn 5 (~120k); 272000 waits until ~240k.
    assert [request["turn"] for request in requests if request["compaction"]] == [first]


def test_auto_profile_turns_still_complete_when_the_runtime_truncates_a_tiny_window(monkeypatch, runtime_path):
    """With a window the verbatim user messages alone fill, the runtime truncates; nothing fails or is lost."""
    _, receipts, _, originals = run_turns(monkeypatch, runtime_path, "auto-tiny", small_capture(AUTO), turns=7)
    assert [status for status, _, _ in receipts] == ["completed"] * 7, receipts
    assert originals["rid-1"].startswith("MARK-TURN-1 ")


def test_without_the_profile_a_runtime_compaction_is_refused_and_the_last_checkpoint_stays(monkeypatch, runtime_path):
    requests, receipts, saved, _ = run_turns(monkeypatch, runtime_path, "none", small_capture(), turns=6)
    assert receipts[-1][:2] == ("unknown", "NATIVE_TURN_UNKNOWN") and len(saved) == len(receipts) - 1
    assert not any(request["compaction"] for request in requests)


@pytest.mark.parametrize("change,code", [
    ("open", "SDK_STATE_SOURCE_UNVERIFIED"), ("manual", "SDK_STATE_SOURCE_UNVERIFIED"),
    ("failed", "SDK_STATE_SOURCE_UNVERIFIED"), ("empty-summary", "SDK_STATE_SOURCE_UNVERIFIED"),
    ("custom", "SDK_STATE_PRIVATE_MATERIAL"),
])
def test_only_complete_successful_automatic_pairs_with_a_summary_are_kept(change, code):
    from shared_context.behavior_sdk_compaction import automatic_pairs
    from shared_context.errors import Problem

    def event(kind, ident, **data):
        return {"type": kind, "id": ident, "timestamp": "2026-10-02T11:00:00.000Z", "parentId": None, "data": data}

    start = event("session.compaction_start", "s1", trigger="threshold")
    complete = event("session.compaction_complete", "c1", trigger="threshold", success=True, summaryContent="<overview>x</overview>")
    assert automatic_pairs([start, complete]) == [start, complete]
    events = {"open": [start], "manual": [{**start, "data": {"trigger": "manual"}}, complete],
              "failed": [start, {**complete, "data": {**complete["data"], "success": False}}],
              "empty-summary": [start, {**complete, "data": {**complete["data"], "summaryContent": " "}}],
              "custom": [{**start, "data": {"trigger": "threshold", "customInstructions": "x"}}, complete]}[change]
    with pytest.raises(Problem) as refused:
        automatic_pairs(events)
    assert refused.value.body["error"]["code"] == code


def responses_provider_double(requests, turn):
    """The openai-responses wire: same summary/reply script, usage that grows with the prompt."""
    from test_instruction_responses_native import response_bytes

    def provider(request):
        assert str(request.url) == URL + "/responses"
        body = json.loads(request.content)
        text = json.dumps(body["input"], ensure_ascii=False)
        compaction = "Please create a detailed summary of the conversation so far" in text
        requests.append({"compaction": compaction, "bytes": len(request.content), "text": text, "turn": turn()})
        reply = ("<overview>Offline summary.</overview>\n<history>1. The user sent MARK-TURN-1 and filler.</history>\n"
                 "<checkpoint_title>Offline filler summary</checkpoint_title>") if compaction else f"REPLY-{len(requests)}"
        tokens = len(request.content) // 4
        raw = response_bytes(len(requests), text=reply).decode().replace(
            '"input_tokens": 21, "output_tokens": 13, "total_tokens": 34',
            f'"input_tokens": {tokens}, "output_tokens": 13, "total_tokens": {tokens + 13}')
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"}, content=raw.encode())
    return provider


@pytest.mark.parametrize("model_provider", [None, "openai-responses"], ids=["completions", "responses"])
def test_the_responses_lane_compacts_saves_and_resumes_the_same_way(local_env, monkeypatch, runtime_path, model_provider):
    """N6: the official Responses host runs the same native turn; auto compaction is saved and the next turn resumes."""
    from shared_context.behavior_native_turn_wire import NATIVE_REQUEST
    from test_native_responses import ResponsesHarness, build_responses_host, response_submit

    root = isolated(monkeypatch, "responses-" + (model_provider or "completions"))
    for key in ("FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FA_RESPONSES_ENABLED", "1")
    monkeypatch.chdir(root)
    requests, receipts = [], []
    provider = (responses_provider_double(requests, lambda: len(receipts) + 1) if model_provider
                else provider_double(requests, lambda: len(receipts) + 1))
    executor = NativeSdkExecutor(root=root / "native", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=URL, token_provider=lambda: "OFFLINE-model-token",
                                 model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    selected = small_capture({**AUTO, "provider": model_provider or "openai-completions"}, prompt=20000,
                             model_provider=model_provider)
    journal = root / "journal"
    journal.mkdir()
    contexts = {}
    with TestClient(build_responses_host(runtime, journal, contexts)) as client:
        harness = ResponsesHarness(runtime, SimpleNamespace(controls={}), client, contexts)
        cid, last = None, None
        for n in range(1, 14):
            text = ("MARK-TURN-1 " if n == 1 else "") + "".join(FILLER.format(i=n * 1000 + j) for j in range(100))
            values = response_submit(rid=f"rid-{n}", cid=cid, text=text).model_dump(mode="json")
            values["capture"] = selected.model_dump(mode="json")
            accepted = harness.invoke(NATIVE_REQUEST.validate_json(json.dumps(values)))
            assert accepted.status_code == 202, accepted.text
            original = harness.call(runtime.read, f"rid-{n}")
            assert original.state_profile == "foundry-responses-v1"
            assert harness.respond(original).status_code == 200

            async def finished(rid=f"rid-{n}"):
                async def poll():
                    while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
                        await asyncio.sleep(0.02)
                    return value
                return await asyncio.wait_for(poll(), timeout=120)

            last = client.portal.call(finished)
            receipts.append((last.status, last.failure_code))
            assert last.status == "completed", receipts
            cid = last.conversation_id

        async def checkpoint():
            meta, _ = await runtime._meta(last.conversation_id)
            async with await open_durable_store(conversation_store_name(last.conversation_id), create=False) as store:
                turn = await runtime._turn(store, last)
                ref = turn.checkpoints[0]
                return ref, await verify_checkpoint(store, runtime._scope(meta), ref,
                                                    checkpoint_profile=last.capture.template.settings.checkpoint_profile)

        ref, state = client.portal.call(checkpoint)
    compacted = [request["turn"] for request in requests if request["compaction"]]
    assert compacted and compacted[0] < 13, "the declared 20000-token prompt limit must make the pinned runtime compact"
    following = [request["text"] for request in requests if request["turn"] == compacted[0] + 1 and not request["compaction"]]
    assert following and "Some of the conversation history has been summarized" in following[0]
    assert "Offline summary" in following[0]
    # TC-06: the Responses lane runs the same native_sdk turn, so the fixed notice follows the same compaction.
    notice = "FA note: earlier parts of this conversation were summarized"
    assert notice in following[0]
    assert not any(notice in request["text"] for request in requests if request["turn"] <= compacted[0])
    assert ref.source is None and ref.codec == ("native-session-fs-responses-v1" if model_provider else "native-session-fs-v1")
    saved = state.files["events.jsonl"]
    kinds = [json.loads(line)["type"] for line in saved.splitlines() if line.strip()]
    assert kinds.count("session.compaction_start") == kinds.count("session.compaction_complete") >= 1
    assert not any(kind in {"model.model_call_success", "model.messages_snapshot"} for kind in kinds)
    assert "PRIVATE_ENCRYPTED_REASONING_CANARY" not in saved




KEEP = "請保留此字串：KEEP-7f3a-逐字-0042"


def test_after_compaction_the_original_wording_is_read_back_exactly_and_history_is_not_rewritten(monkeypatch, runtime_path):
    """TC S3/S4: the summary is derived; the RID receipts, results and history stay the original record."""
    from shared_context.behavior_native_turn_wire import NativeHistory, NativeResult

    root = isolated(monkeypatch, "keep")
    requests, receipts, seen = [], [], {}
    last_turn = 12
    completions = provider_double(requests, lambda: len(receipts) + 1)

    def provider(request):
        body = json.loads(request.content)
        final = len(receipts) + 1 == last_turn and "Please create a detailed summary" not in json.dumps(body["messages"])
        if not final:
            return completions(request)
        requests.append({"compaction": False, "text": json.dumps(body["messages"], ensure_ascii=False),
                         "messages": body["messages"], "turn": last_turn})
        common = {"id": f"offline-final-{len(requests)}", "object": "chat.completion.chunk", "created": 1,
                  "model": "OFFLINE-model"}
        if not any(message.get("role") == "tool" for message in body["messages"]):
            delta, reason = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call-read-first-turn", "type": "function",
                "function": {"name": "read_user_turn", "arguments": json.dumps({"turn_number": 1})},
            }]}, "tool_calls"
        else:
            delta, reason = {"role": "assistant", "content": "QUOTED"}, "stop"
        events = [
            {**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
            {**common, "choices": [], "usage": {"prompt_tokens": len(request.content) // 4, "completion_tokens": 3,
                                                "total_tokens": len(request.content) // 4 + 3}},
        ]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw.encode())

    async def token():
        return "OFFLINE-native-MI"

    executor = NativeSdkExecutor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=URL, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    selected = small_capture(AUTO, prompt=20000)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        cid = None
        for n in range(1, last_turn + 1):
            rid = f"rid-{n}"
            text = ((KEEP + " ") if n == 1 else "") + "".join(FILLER.format(i=n * 1000 + j) for j in range(100))
            request = NativeSubmit(command=submit(rid=rid, cid=cid, text=text).command, capture=selected)
            response = client.post("/invocations", json=request.model_dump(mode="json"), headers={
                "x-client-fa-state-profile": "foundry-native-v1", "x-agent-session-id": IDENTITY.session_id,
                "x-agent-invocation-id": "OFFLINE-" + rid, "x-agent-foundry-call-id": "OFFLINE-call-" + rid,
            })
            assert response.status_code == 202, response.text

            async def finished(rid=rid):
                async def poll():
                    while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
                        await asyncio.sleep(0.02)
                    return value
                return await asyncio.wait_for(poll(), timeout=120)

            receipt = client.portal.call(finished)
            receipts.append(receipt)
            assert receipt.status == "completed", (n, receipt.status, receipt.failure_code)
            cid = receipt.conversation_id

            async def recorded(receipt=receipt):
                page = await runtime.history(NativeHistory(conversation_id=receipt.conversation_id,
                                                           after_turn=receipt.turn_number - 1, limit=1))
                result = await runtime.result(NativeResult(conversation_id=receipt.conversation_id,
                                                           request_id=receipt.request_id))
                return page.turns[0], result

            seen[rid] = (receipt.model_dump(mode="json"), *client.portal.call(recorded))

        async def everything():
            page = await runtime.history(NativeHistory(conversation_id=cid, limit=100))
            results = {}
            for receipt in receipts:
                results[receipt.request_id] = await runtime.result(NativeResult(
                    conversation_id=cid, request_id=receipt.request_id))
            reads = {receipt.request_id: await runtime.read(receipt.request_id) for receipt in receipts}
            return page, results, reads

        page, results, reads = client.portal.call(everything)
    compacted = [request["turn"] for request in requests if request["compaction"]]
    assert compacted and compacted[0] < last_turn, "the runtime must compact before the read-back turn"
    # S3: the model asked for turn 1 and got the original wording back, not the summary's version of it.
    final = [request for request in requests if request["turn"] == last_turn and not request["compaction"]]
    tools = [message for message in final[-1]["messages"] if message.get("role") == "tool"]
    assert len(tools) == 1 and KEEP in tools[0]["content"]
    assert json.loads(tools[0]["content"])["text"] == reads["rid-1"].command.content
    assert reads["rid-1"].command.content.startswith(KEEP + " ")
    # S4: compaction rewrites nothing that was already confirmed; history lists every turn once, in order.
    assert [turn.request_id for turn in page.turns] == [f"rid-{n}" for n in range(1, last_turn + 1)]
    assert page.next_after_turn is None
    for turn in page.turns:
        receipt, public, result = seen[turn.request_id]
        assert reads[turn.request_id].model_dump(mode="json") == receipt
        assert turn == public and results[turn.request_id] == result

