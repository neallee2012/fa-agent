"""從 test_sdk_compaction_product.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import hashlib
import json
import sys
from types import MappingProxyType, SimpleNamespace
from shared_context.behavior_sdk_compaction import NESTED_KEYS, events_from
from shared_context.behavior_sdk_state import CheckpointProfile, SdkState
from shared_context.behavior_work_policy_api import install_work_policy_routes
from work_policy_lifecycle_helpers import enabled


SUMMARY = ("<overview>Original public source: inventory count 850.</overview>"
           "<history>The original source and answer are retained.</history>"
           "<checkpoint_title>Inventory</checkpoint_title>")


INDEX = ("# Checkpoint History\n\nCheckpoints are listed in chronological order. "
         "Checkpoint 1 is the oldest, higher numbers are more recent.\n\n"
         "| # | Title | File |\n|---|-------|------|\n| 1 | Inventory | 001-inventory.md |\n")


def checkpoint_profile():
    return CheckpointProfile(platform={"win32": "win32-x64", "linux": "linux-x64"}[sys.platform])


def event(kind, identity, data, parent=None):
    return {"type":kind,"id":identity,"timestamp":"2026-09-24T00:00:00.000Z","parentId":parent,"data":data}


def ordinary(sid):
    events = [event("session.start","start",{"sessionId":sid}),
              event("user.message","user",{"content":"Read the original inventory source."},"start"),
              event("assistant.message","answer",{"content":"Original public source: inventory count 850."},"user")]
    return SdkState(sdk_session_id=sid,files={
        "events.jsonl":"".join(json.dumps(e)+"\n" for e in events), "workspace.yaml":"id: "+sid+"\n"})


def raw_files(previous):
    events = events_from(previous.files)
    events.extend([
        event("session.compaction_start","compact-start",{"trigger":"manual"},"answer"),
        event("model.model_call_success","diag-call",{
            "kind":"model_call_success","turn":1,"modelCallDurationMs":3,"ttftMs":1.5,"outputTtftMs":1,
            "interTokenLatencyMs":2,"rte":False,"reasoningEffort":"low","toolCount":0,"quotaSnapshots":{},
            "requestMessages":json.dumps([{"role":"system","content":"PRIVATE_FIXTURE_SYSTEM"}]),
            **{key:{name:None for name in keys} for key,keys in NESTED_KEYS.items()},
        },"compact-start"),
        event("model.messages_snapshot","diag-snapshot",{"kind":"messages_snapshot","messages":[
            {"role":"system","content":"PRIVATE_FIXTURE_SYSTEM"},{"role":"user","content":"Public history"}]},"diag-call"),
        event("session.compaction_complete","compact-complete",{
            "trigger":"manual","success":True,"summaryContent":SUMMARY,"checkpointNumber":1,
            "checkpointPath":"/session-state/checkpoints/001-inventory.md","messagesRemoved":2,"tokensRemoved":20,
        },"diag-snapshot"),
    ])
    return {**previous.files,"events.jsonl":"".join(json.dumps(e)+"\n" for e in events),
        "checkpoints/index.md":INDEX,
        "checkpoints/001-inventory.md":SUMMARY.replace("<checkpoint_title>Inventory</checkpoint_title>","")}


def configured(assistant):
    assistant.runtime.profiles = MappingProxyType({key:profile.model_copy(update={"checkpoint_profile":checkpoint_profile()})
                                                  for key,profile in assistant.runtime.profiles.items()})
    h,control,human,_ = enabled(assistant)
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:None,require_current_native_evidence=lambda:None))
    return h,control,human


def settle(turn,state,*,compaction=None):
    reference = turn.client.save_sdk_state(state,turn.stop,compaction=compaction)
    result = turn.client.save_text_reply("Original inventory response.",reference,turn.stop)
    turn.h.executor._complete(turn.start,result.model_dump(mode="json"))
    outcome = turn.h.runtime.controller.step(turn.binding.binding_id)
    assert outcome.state == "succeeded" and outcome.result_forwarded
    return reference


def mutate_events(fs,change):
    values = events_from(fs.files)
    change(values)
    fs.files["events.jsonl"] = "".join(json.dumps(e,ensure_ascii=False)+"\n" for e in values)


PUBLIC_CHECKPOINT_TEXT = (
    '[{"role":"system","content":"You are a pirate."}]',
    '{"systemPrompt":"Answer briefly."}',
    "systemPrompt: Answer briefly.",
    "[" * 2000 + "]" * 2000,
    "before\u2028after",
    "before\u2029after",
    "before\u0085after",
)


def public_checkpoint_content(case, payload, location):
    if location == "summary":
        summary = SUMMARY.replace("Original public source: inventory count 850.", payload)
        mutate_events(case.fs, lambda events: events[-1]["data"].update(summaryContent=summary))
        case.fs.files["checkpoints/001-inventory.md"] = summary.replace(
            "<checkpoint_title>Inventory</checkpoint_title>", "")
        case.fs.compaction = case.fs.compaction.model_copy(
            update={"summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest()})
    else:
        case.fs.files["events.jsonl"] += json.dumps(event(
            location, "public-content", {"content": payload}, "compact-complete"), ensure_ascii=False) + "\n"
