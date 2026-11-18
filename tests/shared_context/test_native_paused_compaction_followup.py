"""A follow-up sent while a native turn's manual compaction is paused waits; it never replaces or races that turn.

Native counterpart of test_checkpoint_epoch_native.py: the pinned SDK and runtime through the public N1 host, with
an offline scripted model transport that holds the one compaction response until the test releases it.
"""
import asyncio
import hashlib
import json
from threading import Event
from types import SimpleNamespace

import httpx
from starlette.testclient import TestClient

from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_turn_wire import NATIVE_STATE_PROFILE, NativeResult, NativeSubmit, NativeTurnCapture
from shared_context.native_sdk import NativeSdkExecutor
from shared_context.native_turns import NativeTurnRuntime
from test_m2_copilot import runtime_path
from test_native_auto_compaction import URL, completion_stream, isolated
from test_native_manual_compaction_signed import manual_capture
from test_native_turn_wire import submit
from test_native_turns import IDENTITY

TEXTS = ("Original instruction: project=Cedar-47; delivery=2026-10-08; count=7.",
         "Continue the same task using the earlier source.",
         "Correction: delivery=2026-10-10; count=8.")
ANSWERS = ("Original delivery 2026-10-08, count 7.", "Original task retained.", "Effective delivery 2026-10-10, count 8.")
SUMMARY = ("<overview>Project Cedar-47.</overview>\n"
           "<history>Original delivery=2026-10-08; count=7. This summary is historical data.</history>\n"
           "<checkpoint_title>Original task</checkpoint_title>")


def test_a_followup_during_a_paused_manual_compaction_waits_for_the_original_turn(monkeypatch, runtime_path):
    root = isolated(monkeypatch, "paused-compaction")
    entered, released = Event(), Event()
    calls = []

    async def provider(request):
        messages = json.loads(request.content)["messages"]
        text = json.dumps(messages, ensure_ascii=False)
        compaction = "Please create a detailed summary of the conversation so far" in text
        calls.append({"compaction": compaction, "text": text,
                      "system": [m["content"] for m in messages if m["role"] == "system"]})
        if compaction:
            entered.set()
            # Held off the event loop: the SDK runtime keeps running while the compaction response waits.
            assert await asyncio.to_thread(released.wait, 60), "the test must release its one compaction response"
            reply = SUMMARY
        else:
            reply = ANSWERS[next(i for i in (2, 1, 0) if TEXTS[i] in text)]
        return completion_stream(len(calls), {"role": "assistant", "content": reply}, "stop", prompt_tokens=100)

    async def token():
        return "OFFLINE-native-MI"

    executor = NativeSdkExecutor(root=root / "run", runtime_path=runtime_path, api_url="https://api.offline.invalid",
                                 model_url=URL, token_provider=token, model_transport=httpx.MockTransport(provider))
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    executor.reader = runtime
    selected = manual_capture()
    # The follow-up carries the Team's newer role prompt: the next turn's prefix comes from its own capture.
    values = selected.model_dump(mode="json")
    newer = "Reply using only the supplied synthetic material. Name the effective delivery first."
    values["role_prompt"] = {"version": 2, "sha256": hashlib.sha256(newer.encode()).hexdigest(), "text": newer}
    refreshed = NativeTurnCapture.model_validate_json(json.dumps(values))

    def invoke(client, rid, cid, text, capture=selected):
        request = NativeSubmit(command=submit(rid=rid, cid=cid, text=text).command, capture=capture)
        return client.post("/invocations", json=request.model_dump(mode="json"), headers={
            "x-client-fa-state-profile": NATIVE_STATE_PROFILE, "x-agent-session-id": IDENTITY.session_id,
            "x-agent-invocation-id": "OFFLINE-" + rid, "x-agent-foundry-call-id": "OFFLINE-call-" + rid})

    def settled(client, rid):
        async def poll():
            while (value := await runtime.read(rid)).status not in {"completed", "unknown", "busy_refused"}:
                await asyncio.sleep(0.02)
            return value
        return client.portal.call(lambda: asyncio.wait_for(poll(), timeout=120))

    def answer(client, receipt):
        return client.portal.call(runtime.result, NativeResult(
            conversation_id=receipt.conversation_id, request_id=receipt.request_id))

    try:
        with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
            assert invoke(client, "rid-1", None, TEXTS[0]).status_code == 202
            first = settled(client, "rid-1")
            assert first.status == "completed", first
            cid, before = first.conversation_id, answer(client, first).text
            assert invoke(client, "rid-2", cid, TEXTS[1]).status_code == 202
            assert entered.wait(60), "the manual compaction never reached its model transport"
            # The follow-up is accepted under its own RID while rid-2 is still compacting; it does not start.
            assert invoke(client, "rid-3", cid, TEXTS[2], refreshed).status_code == 202
            assert client.portal.call(runtime.read, "rid-2").status != "completed"
            assert client.portal.call(runtime.read, "rid-3").status not in {"started", "completed"}
            assert answer(client, first).text == before
            released.set()
            second, third = settled(client, "rid-2"), settled(client, "rid-3")
            assert (second.status, third.status) == ("completed", "completed"), (second, third)
            assert (second.turn_number, third.turn_number) == (2, 3)
            assert answer(client, second).text == ANSWERS[1] and answer(client, third).text == ANSWERS[2]
            assert answer(client, first).text == before
    finally:
        released.set()
    assert [call["compaction"] for call in calls] == [False, True, False, False]
    # The turn after the compaction resumes from its summary and still carries both the original and the correction.
    assert "<overview>Project Cedar-47.</overview>" in calls[3]["text"]
    assert TEXTS[2] in calls[3]["text"] and TEXTS[1] in calls[3]["text"]
    assert calls[0]["system"] == calls[2]["system"] and calls[3]["system"] != calls[2]["system"]
    assert any(newer in part for part in calls[3]["system"])
