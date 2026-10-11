"""ADR-0005 on the pinned SDK 1.0.13 / runtime 1.0.83: a turn that asked the owner keeps its SDK.

The real runtime asks through ``ask_user`` in both turns of one native conversation. The second turn runs on the
same CLI process and session, its question reaches only its own turn's wait, and the model still sees the first
turn's answer. The owner's wait itself is covered offline; model I/O is offline.
"""

import json
from types import SimpleNamespace

from test_m2_copilot import runtime_path  # noqa: F401
from test_native_auto_compaction import completion_stream, run_turns, small_capture
from test_native_resident import phases, track_hitl


def asking_provider(requests, turn):
    """Each turn's first model request asks the owner; the request after the answer replies."""
    asked = set()

    def provider(request):
        body = json.loads(request.content)
        number = turn()
        requests.append({"turn": number, "text": json.dumps(body["messages"], ensure_ascii=False)})
        if number in asked:
            return completion_stream(len(requests), {"role": "assistant", "content": f"REPLY-T{number}"}, "stop",
                                     prompt_tokens=11)
        asked.add(number)
        return completion_stream(len(requests), {"role": "assistant", "content": None, "tool_calls": [{
            "index": 0, "id": f"call-ask-{number}", "type": "function",
            "function": {"name": "ask_user", "arguments": json.dumps({"question": f"QUESTION-T{number}"})}}]},
            "tool_calls", prompt_tokens=11)
    return provider


def test_a_turn_that_asked_the_owner_keeps_the_pinned_runtime_for_the_next_turn(monkeypatch, runtime_path, caplog):
    asked = []

    async def user_input(self, request, invocation):
        self._questions += 1
        number = hitls.index(self) + 1
        asked.append((number, request["question"], invocation["session_id"]))
        return {"answer": f"ANSWER-T{number}", "wasFreeform": True}

    hitls = track_hitl(monkeypatch, user_input)
    requests, receipts, _, _ = run_turns(
        monkeypatch, runtime_path, "resident-ask-user", small_capture(prompt=20000), turns=2, paragraphs=1,
        provider=asking_provider, hitl_client=SimpleNamespace())
    assert [status for status, _, _ in receipts] == ["completed"] * 2, receipts

    assert len(hitls) == 2 and len({sid for _, _, sid in asked}) == 1
    assert [(number, question) for number, question, _ in asked] == [(1, "QUESTION-T1"), (2, "QUESTION-T2")]
    # One runtime process for both turns: the asking turn neither stopped it nor made the next turn restore it.
    assert "sdk_process_start" in phases(caplog, "rid-1")
    assert "sdk_process_stop" not in phases(caplog, "rid-1")
    assert not {"sdk_process_start", "sdk_session_resume", "sdk_process_stop"} & set(phases(caplog, "rid-2"))
    assert not [r.getMessage() for r in caplog.records if r.getMessage().startswith(
        ("RESIDENT_SDK_STOPPED", "RESIDENT_SDK_DISCARDED", "RESIDENT_SDK_RESTARTED", "RESIDENT_FILES_MOVED"))]
    # The kept session carries the first turn's answer into the second turn's model request.
    second = [item["text"] for item in requests if item["turn"] == 2]
    assert second and "ANSWER-T1" in second[0] and "ANSWER-T2" in second[-1]
