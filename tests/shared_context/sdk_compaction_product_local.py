"""One fresh product Hosted client process; only synthetic localhost API traffic."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"src"/"fa-shared-context-poc"))

from pydantic import SecretStr
from copilot import CopilotClient
from shared_context.behavior_assistant_hosted import AssistantClient, AssistantSession
from shared_context.behavior_assistant_wire import Assignment
from shared_context.behavior_hosted import LocalJournal
from shared_context.behavior_checkpoint_privacy import public_events
from shared_context.behavior_sdk_compaction import NESTED_KEYS, TARGETS, events_from
from shared_context.behavior_wire import SdkStartOwner
from shared_context.errors import Problem
from test_behavior_model_diagnostics import LocalHTTP


def main():
    packet=json.loads(sys.stdin.read())
    assignment=Assignment.model_validate_json(json.dumps(packet["assignment"]))
    client=AssistantClient("https://context.invalid",assignment,SecretStr(packet["ticket"]),
        token_provider=lambda:packet["bearer"],transport=LocalHTTP(packet["port"]))
    root = Path(packet["root"].removeprefix("\\\\?\\"))
    gate = {} if assignment.sdk_start_binding is None else {
        "sdk_journal": LocalJournal(root),
        "sdk_owner": SdkStartOwner(kind="local-offline", supervisor_id=assignment.session_id),
    }
    session=AssistantSession(client,root=root,runtime_path=Path(packet["runtime"]), **gate)
    force_stops=0
    force_stop=CopilotClient.force_stop
    async def observe_force_stop(instance):
        nonlocal force_stops
        force_stops+=1
        return await force_stop(instance)
    CopilotClient.force_stop=observe_force_stop
    try:
        outcome=session.dispatch({"operation":"assistant","profile":"assistant-v1.2","action":"start"})
        assert outcome["state"]=="ready_to_finalize", "product result was not durably finalized"
        fs=session._session_files
        state=fs.snapshot()
        raw=events_from(fs.files)
        if fs.compaction is not None:
            assert all(e["data"]!={} for e in raw if e["type"] in TARGETS), "raw SDK diagnostic data was rewritten"
        expected,_=public_events(fs.files["events.jsonl"])
        for event in expected:
            if event["type"] in TARGETS:event["data"]={}
        assert events_from(state.files)==expected, "product projection changed envelopes, order or non-target data"
        assert all(state.files[name]==text for name,text in fs.files.items() if name!="events.jsonl")
        encoded=state.encoded()
        # The private system prompt of this assignment's actual profile (v2 is the unconfigured default).
        turn=assignment.task.instruction
        profile=None if turn is None else turn.prompt_profile
        policy=("Never acquire permissions, delete data, publish," if profile is None
                else "FA instruction profile: "+profile+".")
        assert any(policy in text for text in fs.files.values()), "actual private SDK system event was not exercised"
        assert policy not in encoded.decode()
        assert all(secret not in encoded.decode() for secret in client._checkpoint_secrets)
        tool_events=[e for e in events_from(state.files) if e["type"] in {"tool.execution_start","tool.execution_complete"}]
        proof={
            "sdk_version":importlib.metadata.version("github-copilot-sdk"),"force_stop_calls":force_stops,
            "python_pid":os.getpid(),"sdk_stop":outcome["sdk_stop"],"sdk_session_id":state.sdk_session_id,
            "conversation_id":assignment.task.instruction.conversation_id,"job_id":assignment.task.job_id,
            "codec":state.codec,"state_bytes":len(encoded),"zlib_comparison_bytes":len(zlib.compress(encoded)),
            "state_sha256":state.sha256,"event_count":len(raw),"projected_event_ids":state.projected_event_ids,
            "compaction_invoked":fs.compaction is not None,"raw_not_rewritten":events_from(fs.files)==raw,
            "provider_metadata": [{
                "chunk_fields": sorted(e["data"]["responseChunk"].keys() & {
                    "system_fingerprint", "service_tier", "prompt_filter_results", "content_filter_results",
                    "latency_checkpoint", "routing", "obfuscation"}),
                "usage_details": sorted(e["data"]["responseUsage"].keys() & {
                    "prompt_tokens_details", "completion_tokens_details"}),
                "choice_filter_present": any("content_filter_results" in choice
                    for choice in e["data"]["responseChunk"].get("choices", [])),
                "reasoning_tokens": (e["data"]["responseUsage"].get("completion_tokens_details") or {}).get("reasoning_tokens"),
            } for e in raw if e["type"] == "model.model_call_success" and e["data"]],
            "native_compaction":[{"type":e["type"],"id":e["id"],"trigger":e["data"].get("trigger"),
                "custom_instructions_present":"customInstructions" in e["data"],
                "request_id_present":"requestId" in e["data"],"service_request_id_present":"serviceRequestId" in e["data"]}
                for e in raw if e["type"].startswith("session.compaction")],
            "private_system_exercised_and_filtered":True,"transport_credentials_absent":True,
            "tool_events":[{"type":e["type"],"id":e["id"],
                "data_sha256":hashlib.sha256(json.dumps(e["data"],sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
                for e in tool_events],
            "outcome":outcome,
        }
        print("CTX03_PROOF "+json.dumps(proof,ensure_ascii=False),flush=True)
    except Problem as error:
        shape_checks = []
        if session._session_files is not None and "events.jsonl" in session._session_files.files:
            for record in events_from(session._session_files.files):
                if record["type"] == "model.model_call_success":
                    shape_checks.append({
                        field: {"expected_count": len(keys),
                                "actual_count": len(record["data"].get(field, {})),
                                "missing": sorted(keys - record["data"].get(field, {}).keys()),
                                "extra_count": len(record["data"].get(field, {}).keys() - keys),
                                "provider_fields": sorted(record["data"].get(field, {}).keys() & {
                                    "system_fingerprint", "service_tier", "prompt_filter_results", "content_filter_results",
                                    "prompt_tokens_details", "completion_tokens_details"})}
                        for field, keys in NESTED_KEYS.items()
                        if isinstance(record["data"].get(field), dict)
                    })
        print("CTX03_ERROR "+json.dumps({"error":error.body["error"],
            "filesystem_failed":None if session._session_files is None else session._session_files.failed,
            "checkpoint_error":None if session._session_files is None else session._session_files.checkpoint_error,
            "diagnostic_shape_checks": shape_checks}),flush=True)
        raise
    finally:
        client.close()


if __name__=="__main__":
    main()
