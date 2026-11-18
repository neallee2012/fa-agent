"""A short native conversation with the manual checkpoint profile: the runtime's signed compaction metadata is saved
as it reported it, and the next turn restores and inherits it.

Native counterpart of test_sdk_compaction_signed_native.py: the pinned SDK and runtime through the public N1 host,
with an offline scripted model transport (the harness of test_native_auto_compaction.py).
"""
import json

from shared_context.behavior_instruction_models import InstructionModel
from shared_context.behavior_native_turn_wire import NativeTurnCapture
from shared_context.behavior_sdk_compaction import events_from
from test_m2_copilot import runtime_path
from test_native_auto_compaction import PLATFORM, completion_stream, run_turns, small_capture

MANUAL = {"name": "manual-no-custom-v1", "platform": PLATFORM, "provider": "openai-completions"}
SUMMARY = ("<overview>Original public conversation.</overview>"
           "<history>A fixture reply was saved.</history>"
           "<checkpoint_title>Public conversation</checkpoint_title>")
REPLIES = ("Offline fixture reply; no external action succeeded.", "Offline second fixture reply.",
           "Restored the saved short conversation.")


def scripted(calls, turn):
    def provider(request):
        text = json.dumps(json.loads(request.content)["messages"], ensure_ascii=False)
        compaction = "Please create a detailed summary of the conversation so far" in text
        calls.append({"turn": turn(), "compaction": compaction, "text": text})
        reply = SUMMARY if compaction else REPLIES[turn() - 1]
        return completion_stream(len(calls), {"role": "assistant", "content": reply}, "stop", prompt_tokens=100)
    return provider


def manual_capture():
    """The legacy fixture's Chat Completions model; the runtime's diagnostics shape depends on the deployment."""
    values = small_capture(MANUAL).model_dump(mode="json")
    model = InstructionModel(id="gpt-4.1-mini", display_name="gpt-4.1-mini", context_window_tokens=6000,
                             max_prompt_tokens=5000, max_output_tokens=512)
    values["model"], values["model_capacity"] = (model.model_dump(mode="json"),
                                                 model.capacity_snapshot().model_dump(mode="json"))
    return NativeTurnCapture.model_validate_json(json.dumps(values))


def test_short_native_manual_compaction_keeps_signed_metadata_and_inherited_restore(monkeypatch, runtime_path):
    calls, receipts, saved, originals = run_turns(monkeypatch, runtime_path, "manual-signed",
                                                  [manual_capture()] * 3, paragraphs=1, provider=scripted)
    assert [status for status, _, _ in receipts] == ["completed"] * 3, receipts
    # The manual profile compacts once, when turn 2 resumes the ordinary turn-1 state; turn 3 resumes the compacted one.
    assert [(call["turn"], call["compaction"]) for call in calls] == [(1, False), (2, True), (2, False), (3, False)]
    assert "<overview>Original public conversation.</overview>" in calls[2]["text"]
    assert REPLIES[1] in calls[3]["text"] and "<overview>Original public conversation.</overview>" in calls[3]["text"]
    (first, _), (second, compacted), (third, restored) = saved
    assert first.source is None and first.codec == "native-session-fs-v1"
    assert second.codec == third.codec == "native-session-fs-diag-empty-v1"
    execution = second.source.execution
    complete = next(e for e in events_from(compacted.files) if e["type"] == "session.compaction_complete")
    assert type(complete["data"]["tokensRemoved"]) is int
    assert execution.tokens_removed == complete["data"]["tokensRemoved"]
    assert execution.messages_removed == complete["data"]["messagesRemoved"] and execution.trigger == "manual"
    assert third.source.execution == execution
    assert (third.lineage.previous_request_id, second.lineage.previous_request_id) == ("rid-2", "rid-1")
    assert events_from(restored.files)[:len(events_from(compacted.files))] == events_from(compacted.files)
    assert all(e["data"] == {} for e in events_from(compacted.files)
               if e["type"] in {"model.model_call_success", "model.messages_snapshot"})
    assert len(originals) == 3
