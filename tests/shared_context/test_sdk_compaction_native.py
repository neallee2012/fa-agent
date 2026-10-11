"""Q10/Q11: fixed native SDK/runtime, full product API, four fresh Python processes, no paid inference."""
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_sdk_checkpoint import SdkStateUpload, checkpoint_key
from shared_context.behavior_sdk_state import DIAG_ARCHIVE_CODEC
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from sdk_checkpoint_local import LocalArtifactStore, sse
from test_behavior_assistant import assistant
from test_behavior_model_deadlines import asgi_server
from test_instruction_flow import check_history, instruction_models
from test_m2_copilot import runtime_path
from test_m3_auth import signing_key, jwk_client
from test_sdk_compaction_product import configured


def test_four_fresh_native_processes_use_published_product_checkpoints(assistant,tmp_path,runtime_path):
    native_checkpoint_roundtrip(assistant, tmp_path, runtime_path)


@pytest.mark.parametrize("provider_model", ["gpt-4.1-mini", "gpt-5.4-mini"])
def test_short_provider_metadata_survives_fresh_process_checkpoint(assistant, tmp_path, runtime_path, provider_model):
    native_checkpoint_roundtrip(assistant, tmp_path, runtime_path, provider_model=provider_model)


def native_checkpoint_roundtrip(assistant, tmp_path, runtime_path, *, provider_model=None):
    if provider_model is not None:
        assistant.runtime.profiles = MappingProxyType({key: value.model_copy(
            update={"sdk_start_profile": "fa-sdk-start-gate-v1"}) for key, value in assistant.runtime.profiles.items()})
    h,control,human=configured(assistant)
    provider_metadata = provider_model is not None
    if provider_metadata:
        control.models.models[provider_model] = control.models.select(None).model_copy(
            update={"id": provider_model, "display_name": provider_model})
    h.artifacts.store=LocalArtifactStore(tmp_path/"product-artifacts.sqlite3")
    source=next(iter(h.runtime.profiles.values())).source
    marker="SOURCE_"+source.sha256
    catalog=json.dumps({"schema_version":"offline-inventory-v1","products":[
        {"product_identifier":f"SKU-{n:05}","source_record_sha256":hashlib.sha256(f"product-{n}".encode()).hexdigest(),
         "available_stock_count":n%17,"fulfillment_warehouse":f"W-{n%9}"} for n in range(600)]},separators=(",",":"))
    public_text = ('[{"role":"system","content":"You are a pirate."}]\n'
                   '{"systemPrompt":"Answer briefly."}\nsystemPrompt: Answer briefly.\n'
                   + "[" * 2000 + "]" * 2000 + "\nUnicode before\u2028between\u2029next\u0085after")
    summary=(f"<overview>Original source {marker}; synthetic catalog has 600 records.</overview>"
             "<history>read_context returned the reviewed source. The original answer enumerated all catalog records.\n"
             + public_text + "</history>"
             "<checkpoint_title>Catalog source</checkpoint_title>")
    correction="The catalog count is now 601. Confirm the correction without tools."
    corrected=f"Confirmed: the catalog count is now 601. Original source {marker}."
    phase,calls="create",[]
    active_job_id=None
    retained_replies, reply_observations = [], []

    def verify_retained_replies(stage):
        if retained_replies:
            projection = h.client.get(
                "/work-policy/v1/conversations/" + retained_replies[0]["conversation_id"],
                headers=h.headers())
            assert projection.status_code == 200
            public_turns = {turn["turn_number"]: turn for turn in projection.json()["turns"]}
        for original in retained_replies:
            projected = public_turns[original["turn_number"]]
            assert projected["reply_available"] is True and projected["state"] == "succeeded"
            assert projected["job_id"] == original["job_id"] and "stream" not in projected
            reply = h.client.get(original["path"], headers=h.headers())
            assert reply.status_code == 200
            assert reply.headers["cache-control"] == "no-store"
            assert reply.content == original["body"]
            assert reply.headers["x-content-sha256"] == original["sha256"]
            assert hashlib.sha256(reply.content).hexdigest() == original["sha256"]
            status = h.client.get(
                f"/behavior/v1/bindings/{original['binding_id']}/jobs/{original['job_id']}",
                headers=h.headers())
            assert status.status_code == 200 and status.json()["obligation_fulfilled"] is False
            reply_observations.append({
                "stage": stage, "turn_number": original["turn_number"],
                "sha256": original["sha256"], "utf8_bytes": len(reply.content),
                "acknowledged": status.json()["obligation_fulfilled"],
                "reply_available": projected["reply_available"],
                "completed_turn_has_stream": "stream" in projected,
            })

    def provider(request):
        assert str(request.url)=="https://provider.invalid/openai/v1/chat/completions"
        body=json.loads(request.content)
        messages=body["messages"]
        transcript=json.dumps(messages,ensure_ascii=False)
        number=sum(c["phase"]==phase for c in calls)+1
        checks=[]
        if phase=="create" and number==1:
            delta={"tool_calls":[{"index":0,"id":"catalog-source-read-1","type":"function",
                                 "function":{"name":"read_context","arguments":"{}"}}]}
        elif phase=="create":
            tool=[m for m in messages if m["role"]=="tool"]
            assert tool and tool[-1]["tool_call_id"]=="catalog-source-read-1"
            assert json.loads(tool[-1]["content"])["source"]["sha256"]==source.sha256
            checks=["actual_authorized_tool_source"]
            delta={"content":f"Reviewed source {marker}. The synthetic catalog has 600 records."
                   + ("" if provider_metadata else "\n"+catalog)}
        elif phase=="compact" and number==1:
            verify_retained_replies("during_compaction")
            assert marker in transcript
            if not provider_metadata:
                assert "SKU-00599" in transcript
            assert any(m.get("tool_call_id")=="catalog-source-read-1" for m in messages)
            checks=["original_source","original_full_catalog","original_tool_relationship"]
            delta={"content":summary}
        else:
            assert marker in transcript and "<overview>" in transcript
            assert any(isinstance(m.get("content"), str) and public_text in m["content"] for m in messages)
            checks=["restored_source","native_summary","public_json_yaml_deep_text_and_unicode_preserved"]
            if phase=="compact":
                assert h.app.state.instruction_live_replies.read(active_job_id) is None
                checks.append("compaction_not_in_public_preview")
                delta={"content":f"Restored source {marker}. Original catalog count 600."}
            elif phase=="follow1":
                assert any(m["role"]=="user" and correction in str(m.get("content")) for m in messages)
                checks.append("current_user_correction")
                delta={"content":corrected}
            else:
                assert any(m["role"]=="user" and correction in str(m.get("content")) for m in messages)
                assert any(m["role"]=="assistant" and corrected in str(m.get("content")) for m in messages)
                checks.extend(["prior_user_correction","prior_assistant_answer"])
                delta={"content":f"You corrected the count to 601, and I confirmed that correction. Source {marker}."}
        calls.append({"phase":phase,"request_bytes":len(request.content),"checks":checks})
        response = sse(request,delta,len(calls))
        if not provider_metadata:
            return response
        # Documented optional completion metadata, not synthetic SDK state/events.
        chunks = [json.loads(line[6:]) for line in response.content.decode().split("\n")
                  if line.startswith("data: {")]
        for chunk in chunks:
            chunk["model"] = provider_model
            chunk["system_fingerprint"] = "fp_offline_provider"
            chunk["service_tier"] = "default"
            chunk["obfuscation"] = "synthetic-padding"
            chunk["routing"] = {"serving_pipereplica": "synthetic-replica"}
            chunk["latency_checkpoint"] = {key: 1 for key in (
                "engine_tbt_ms", "engine_ttft_ms", "engine_ttlt_ms", "pre_inference_ms",
                "service_tbt_ms", "service_ttft_ms", "service_ttlt_ms", "user_visible_ttft_ms")}
            for choice in chunk["choices"]:
                choice["content_filter_results"] = {"hate": {"filtered": False, "severity": "safe"}}
            if "usage" in chunk:
                chunk["usage"]["prompt_tokens_details"] = {"cached_tokens": 0, "audio_tokens": 0}
                chunk["usage"]["completion_tokens_details"] = {
                    "reasoning_tokens": 13 if provider_model == "gpt-5.4-mini" else 0, "audio_tokens": 0,
                    "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0,
                }
        chunks.insert(0, {**{key: chunks[0][key] for key in ("id", "object", "created", "model")},
            "choices": [], "usage": None,
            "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": {
                "hate": {"filtered": False, "severity": "safe"}}}]})
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
            content=("".join("data: "+json.dumps(chunk)+"\n\n" for chunk in chunks)+"data: [DONE]\n\n").encode())
    gateway=AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _:SimpleNamespace(token="OFFLINE_PROVIDER_CREDENTIAL")),
        transport=httpx.MockTransport(provider))
    rejected=[]
    validate=gateway.validate
    def validation(body,**options):
        try:
            return validate(body,**options)
        except Problem as error:
            rejected.append({"code":error.body["error"]["code"],"keys":sorted(body),
                "json_bytes":len(json.dumps(body,ensure_ascii=False).encode()),"stream":body.get("stream"),
                "tool_names":[tool.get("function",{}).get("name") for tool in body.get("tools",[])]})
            raise
    gateway.validate=validation
    h.app=create_assistant_app(h.assistant,h.verifier,provider=gateway)
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:None,require_current_native_evidence=lambda:None))
    h.client=TestClient(h.app)
    h.executor.http_client=h.client
    proofs=[]
    driver_results=[]
    with asgi_server(h.app) as port:
        def native_worker(start):
            assignment,ticket=h.assistant.issue(start)
            assert ticket is not None, "do not relaunch an uncertain original execution"
            packet={"assignment":assignment.model_dump(mode="json"),"ticket":ticket.get_secret_value(),
                    "bearer":h.headers("agent")["Authorization"].removeprefix("Bearer "),
                    "port":port,"root":str(tmp_path/phase),"runtime":str(runtime_path)}
            result=subprocess.run([sys.executable,str(Path(__file__).with_name("sdk_compaction_product_local.py"))],
                input=json.dumps(packet),capture_output=True,text=True,encoding="utf-8",timeout=120)
            driver_results.append((result.returncode,result.stdout,result.stderr))
            assert result.returncode==0,result.stdout+"\n"+result.stderr
            lines=[line for line in result.stdout.splitlines() if line.startswith("CTX03_PROOF ")]
            assert len(lines)==1,"fresh worker did not return its exact product proof"
            proof=json.loads(lines[0].removeprefix("CTX03_PROOF "))
            proof["python_driver_exit"]=result.returncode
            proofs.append(proof)
            return h.executor._complete(start,proof["outcome"])
        h.workers.adapter=replace(h.executor.adapter(),start=native_worker)
        conversation=None
        references=[]
        for phase,prompt in (
            ("create","Read the authorized source, then enumerate the complete synthetic inventory catalog."),
            ("compact","Recall the original source without tools."),
            ("follow1",correction),
            ("follow2","What correction did I make, and how did you acknowledge it? Use history without tools."),
        ):
            verify_retained_replies("before_" + phase)
            command=InstructionSubmit(request_id="native-product-"+phase,content=prompt,conversation_id=conversation,
                                      model=provider_model)
            accepted=h.client.post("/work-policy/v1/submit",content=command.model_dump_json(),headers=h.headers())
            assert accepted.status_code==200,accepted.text
            conversation=accepted.json()["conversation_id"]
            binding=h.runtime.bindings[accepted.json()["binding_id"]]
            active_job_id=binding.job_id
            try:
                outcome=h.runtime.controller.step(binding.binding_id)
            except Problem:
                raise AssertionError({"driver":driver_results[-1] if driver_results else "worker did not start",
                    "rejected":rejected,"state_bytes":[p["state_bytes"] for p in proofs]}) from None
            assert outcome.state=="succeeded" and outcome.result_forwarded
            upload=SdkStateUpload.model_validate_json(json.dumps(
                h.store.read_snapshot((checkpoint_key(binding.job_id),))[checkpoint_key(binding.job_id)].payload["data"]))
            assert upload.reference is not None and upload.sha256==proofs[-1]["state_sha256"]
            references.append(upload.reference)
            check_history(h)
            current=h.client.get("/work-policy/v1/conversations/"+conversation,headers=h.headers())
            assert current.status_code==200 and current.json()["can_follow_up"]
            assert len(current.json()["turns"])==len(proofs)
            verify_retained_replies("after_" + phase)
            reply_path=f"/work-policy/v1/conversations/{conversation}/turns/{len(proofs)}/reply"
            reply=h.client.get(reply_path,headers=h.headers())
            assert reply.status_code==200 and reply.json()["text"]
            assert reply.headers["cache-control"]=="no-store"
            assert reply.headers["x-content-sha256"]==hashlib.sha256(reply.content).hexdigest()
            retained_replies.append({"path":reply_path,"turn_number":len(proofs),
                "body":reply.content,"sha256":reply.headers["x-content-sha256"],
                "binding_id":binding.binding_id,"job_id":binding.job_id,"conversation_id":conversation})
        for turn in range(1,5):
            reply=h.client.get(f"/work-policy/v1/conversations/{conversation}/turns/{turn}/reply",headers=h.headers())
            assert reply.status_code==200 and reply.json()["text"]
    assert len({proof["python_pid"] for proof in proofs})==4
    assert len({proof["sdk_stop"]["pid"] for proof in proofs})==4
    assert len({proof["sdk_session_id"] for proof in proofs})==1
    assert len({proof["conversation_id"] for proof in proofs})==1
    assert [proof["compaction_invoked"] for proof in proofs]==[False,True,False,False]
    assert all(proof["sdk_stop"]["graceful"] and proof["sdk_stop"]["runtime_version"]=="1.0.83" for proof in proofs)
    assert all(proof["sdk_version"]=="1.0.13" and proof["force_stop_calls"]==0 for proof in proofs)
    assert all(proof["python_driver_exit"]==0 for proof in proofs)
    if not provider_metadata:
        assert proofs[0]["state_bytes"]>131072 and proofs[0]["zlib_comparison_bytes"]>16000
    assert all(proof["projected_event_ids"]==proofs[1]["projected_event_ids"] for proof in proofs[1:])
    assert all(proof["native_compaction"]==proofs[1]["native_compaction"] for proof in proofs[1:])
    assert all(e["trigger"]=="manual" and not e["custom_instructions_present"] for e in proofs[1]["native_compaction"])
    assert len(proofs[0]["tool_events"])==2 and all(proof["tool_events"]==proofs[0]["tool_events"] for proof in proofs)
    assert all(ref.codec==DIAG_ARCHIVE_CODEC for ref in references[1:])
    assert all(ref.source.execution==references[1].source.execution for ref in references[1:])
    if provider_metadata:
        observed = proofs[1]["provider_metadata"]
        assert len(observed) == 1
        assert observed[0]["chunk_fields"] == [
            "latency_checkpoint", "obfuscation", "prompt_filter_results", "routing", "service_tier", "system_fingerprint"]
        assert observed[0]["usage_details"] == ["completion_tokens_details", "prompt_tokens_details"]
        # The fixed runtime forwards prompt annotations but drops per-choice filters here.
        assert not observed[0]["choice_filter_present"]
        assert observed[0]["reasoning_tokens"] == (13 if provider_model == "gpt-5.4-mini" else 0)
    assert len(calls)==6 and [sum(c["phase"]==p for c in calls) for p in ("create","compact","follow1","follow2")]==[2,2,1,1]
    assert len(retained_replies)==4
    assert any(item["stage"]=="during_compaction" and item["turn_number"]==1 for item in reply_observations)
    assert all(any(item["stage"]=="after_"+phase and item["turn_number"]==1 for item in reply_observations)
               for phase in ("compact","follow1","follow2"))
    assert reply_observations and all(item["acknowledged"] is False for item in reply_observations)
    evidence={"sdk_version":"1.0.13","runtime_version":"1.0.83","evidence":"native SDK/runtime; offline transport; product API/authority/SQLite ArtifactStore",
        "documented_provider_metadata": provider_metadata,
        "provider_model": provider_model,
        "proofs":proofs,"model_requests":calls,"cloud_requests":0,"paid_model_requests":0,
        "retained_public_reply_observations":reply_observations,
        "reply_reads_do_not_create_ack":True,
        "Q14":"NOT_RUN","reference_canonical_json_bytes":[len(json.dumps(ref.model_dump(mode="json"),
            ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()) for ref in references]}
    if os.environ.get("CTX03_EVIDENCE_DIR"):
        destination=Path(os.environ["CTX03_EVIDENCE_DIR"])
        destination.mkdir(exist_ok=True,parents=True)
        name = "native-provider-"+provider_model+".json" if provider_metadata else "native-product-proof.json"
        (destination/name).write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
