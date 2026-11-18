"""A provider stream that fails mid-body after several tool rounds ends the native turn; nothing is resent.

Native counterpart of test_stream_failure_turn_end.py: the pinned SDK and runtime through the public N1 host, with
an offline scripted model transport (the harness of test_native_auto_compaction.py).
"""
import json
import time

import httpx

from test_m2_copilot import runtime_path
from test_native_auto_compaction import completion_stream, run_turns, small_capture

ROUNDS = 3


class _Broken(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"OFFLINE-model","choices":[]}\n\n'
        raise httpx.ReadError("upstream stream aborted")


def scripted(requests, turn=lambda: None):
    def provider(request):
        requests.append(json.loads(request.content))
        index = len(requests)
        if index <= ROUNDS:
            return completion_stream(index, {"role": "assistant", "tool_calls": [{
                "index": 0, "id": f"call-{index}", "type": "function",
                "function": {"name": "read_context", "arguments": "{}"}}]}, "tool_calls", prompt_tokens=100)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Broken(), request=request)
    return provider


def test_a_provider_stream_failure_after_tool_rounds_ends_the_native_turn(monkeypatch, runtime_path):
    began = time.monotonic()
    requests, receipts, saved, _ = run_turns(monkeypatch, runtime_path, "stream-fail", [small_capture()],
                                             paragraphs=1, provider=scripted)
    elapsed = time.monotonic() - began
    assert len(requests) == ROUNDS + 1, "the failed model request was resent"
    assert receipts == [("unknown", "NATIVE_TURN_UNKNOWN", 1)] and saved == [], receipts
    assert elapsed < 60
