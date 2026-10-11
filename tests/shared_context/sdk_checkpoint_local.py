"""Cross-process local ArtifactStore and scripted model; never an Azure adapter."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import zlib

import httpx

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_assistant_wire import InstructionContext
from shared_context.behavior_sdk_checkpoint import SdkCheckpointStore
from shared_context.behavior_sdk_state import StoredSdkArchive
from shared_context.behavior_sdk_files import SdkSessionFiles, SESSION_FS
from shared_context.m2_copilot import run_copilot, ToolSpec, ToolOutput
from shared_context.m3_models import BlobReceipt
from shared_context.m3_store import _validate_upload, _match_receipt, _verify_content
from shared_context.errors import Problem


class LocalArtifactStore:
    """Same create-only interface, SQLite durability only for this offline test."""
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS versions (team TEXT, artifact TEXT, version TEXT, receipt TEXT, data BLOB, PRIMARY KEY(team,artifact,version))")

    def put(self, team, artifact, version, data, sha256):
        _validate_upload(team, artifact, version, data, sha256)
        receipt = BlobReceipt(native_version="local-" + version, etag='"' + sha256 + '"', sha256=sha256, bytes=len(data))
        with sqlite3.connect(self.path) as db:
            try:
                db.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (team, artifact, version, receipt.model_dump_json(), data))
            except sqlite3.IntegrityError:
                raise Problem(409, "ARTIFACT_VERSION_EXISTS", "original local artifact already exists") from None
        return receipt

    def read(self, team, artifact, version, receipt):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT receipt,data FROM versions WHERE team=? AND artifact=? AND version=?",
                             (team, artifact, version)).fetchone()
        if row is None:
            raise Problem(404, "ARTIFACT_BLOB_NOT_FOUND", "local checkpoint part is absent")
        _match_receipt(BlobReceipt.model_validate_json(row[0]), receipt)
        _verify_content(receipt, row[1])
        return row[1]


def catalog():
    return json.dumps({"schema_version": "offline-catalog-v1", "products": [
        {"product_id": f"SKU-{n:05}", "source_digest": hashlib.sha256(f"product-{n}".encode()).hexdigest(),
         "stock": n % 17, "warehouse": f"W-{n%9}"} for n in range(850)]}, separators=(",", ":"))


def sse(request, delta, count):
    base = {"id": f"offline-{count}", "object": "chat.completion.chunk", "created": 1, "model": "gpt-4.1-mini"}
    reason = "tool_calls" if "tool_calls" in delta else "stop"
    chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", **delta}, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
              {**base, "choices": [], "usage": {"prompt_tokens": 1500, "completion_tokens": 100, "total_tokens": 1600}}]
    return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
        content=("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode())


async def execute(root, phase, runtime):
    store = SdkCheckpointStore(LocalArtifactStore(root / "artifacts.sqlite3"))
    binding = SimpleNamespace(tenant_id="11111111-1111-4111-8111-111111111111", team_id="offline-team",
                              agent_oid="offline-agent", job_id="job-" + phase)
    sid, cid = "ctx03-native-session", "ctx03-native-conversation"
    previous = None
    if phase != "create":
        prior_phase="compact" if phase=="resume_compacted" else "create"
        previous = StoredSdkArchive.model_validate_json((root / (prior_phase+"-reference.json")).read_bytes())
        state = store.load(previous, binding, conversation_id=cid, sdk_session_id=sid, job_id="job-"+prior_phase)
        fs = SdkSessionFiles(sid, state)
    else:
        fs = SdkSessionFiles(sid)
    calls, tools = [], []
    source = catalog()
    token = hashlib.sha256(source.encode()).hexdigest()
    marker = "CATALOG_" + token[:20]
    credential_canary = "CTX03_TRANSPORT_CREDENTIAL_CANARY"
    private_system = "CTX03_PRIVATE_SYSTEM_" + phase.upper()

    async def read_context(_):
        tools.append("read_context")
        manifest=json.dumps({"schema_version":"offline-catalog-source-v1","products":850,
            "catalog_sha256":token,"generation":"product_id SKU-00000..SKU-00849; source_digest sha256(product-N); stock N%17; warehouse W-(N%9)"})
        return ToolOutput(InstructionContext(source={
            "source_id": "offline-products", "version": 1, "sha256":hashlib.sha256(manifest.encode()).hexdigest(),
            "byte_length":len(manifest.encode())},content=manifest).model_dump_json())

    def respond(request):
        assert str(request.url) == "https://offline.invalid/model/v1/chat/completions"
        body = json.loads(request.content)
        assert request.headers["authorization"]=="Bearer "+credential_canary
        calls.append(body)
        if phase == "create" and len(calls) == 1:
            return sse(request, {"tool_calls": [{"index": 0, "id": "catalog-read-1", "type": "function",
                "function": {"name": "read_context", "arguments": "{}"}}]}, len(calls))
        if phase == "create":
            results = [m for m in body["messages"] if m["role"] == "tool"]
            assert results and results[-1]["tool_call_id"] == "catalog-read-1"
            assert results[-1]["content"].find("850")>=0, "the real source tool did not complete"
            return sse(request, {"content": "The inventory catalog has 850 products. Source " + marker+"\n"+source}, len(calls))
        transcript = json.dumps(body["messages"], ensure_ascii=False)
        assert "CTX03_PRIVATE_SYSTEM_CREATE" not in transcript, "private prior system text returned on resume"
        assert marker in transcript, "resume lost the original catalog summary"
        if phase == "resume":
            assert any(m.get("tool_calls") for m in body["messages"]), "resume lost tool call relationships"
            assert any(m["role"] == "tool" and m.get("tool_call_id") == "catalog-read-1" for m in body["messages"])
        return sse(request, {"content": "Restored original source " + marker}, len(calls))

    async def reserve(_):
        pass

    work = root / ("work-" + phase)
    work.mkdir()
    if phase == "compact":
        from copilot import CopilotClient, RuntimeConnection, ToolSet, CopilotRequestHandler
        compaction = []
        class Model(CopilotRequestHandler):
            async def send_request(self, request, ctx):
                assert request.url.host == "offline.invalid"
                packet = json.loads(request.content)
                calls.append(packet)
                summary = ("<overview>Inventory catalog with 850 products " + marker + "</overview>"
                    "<history>Read the generation contract using read_context and produced the complete catalog.</history>"
                    "<checkpoint_title>Original catalog</checkpoint_title>")
                return sse(request, {"content": summary}, len(calls))
        client = CopilotClient(connection=RuntimeConnection.for_stdio(path=str(runtime)), mode="empty",
            base_directory=str(work), working_directory=str(work), use_logged_in_user=False,
            log_level="error",
            session_fs=SESSION_FS, request_handler=Model(),
            env={"SYSTEMROOT": os.environ.get("SYSTEMROOT", ""), "HOME": str(work), "COPILOT_SKIP_CLI_DOWNLOAD": "1"})
        session = None
        try:
            await client.start()
            status = await client.get_status()
            assert status.version == "1.0.83"
            process = client._cli_process
            session = await client.resume_session(sid, continue_pending_work=False, model="gpt-4.1-mini",
                available_tools=ToolSet().add_custom("read_context"), enable_config_discovery=False,
                enable_managed_settings=False, create_session_fs_handler=lambda _: fs, streaming=True,
                on_permission_request=lambda *_: None,
                system_message={"mode": "replace", "content": "Offline persistence test."},
                provider={"type": "openai", "wire_api": "completions", "base_url": "https://offline.invalid/model/v1",
                          "api_key": "OFFLINE"})
            session.on(lambda e: compaction.append(e) if e.type.value == "session.compaction_complete" else None)
            outcome = await asyncio.wait_for(session.rpc.history.compact(), 30)
            assert outcome.success and any(e.data.success for e in compaction), "native compaction not successful"
        finally:
            if session is not None:
                await session.disconnect()
            await client.stop()
        result = {"final_message": "Native compaction completed", "sdk_stop":
                  {"runtime_version": "1.0.83", "pid": process.pid, "exit_code": process.returncode, "graceful": True}}
        native_compaction = [e.data.to_dict() for e in compaction]
    else:
        result = await run_copilot(state_dir=root / ("sdk-" + phase), working_directory=work, sdk_session_id=sid,
            resume=phase != "create", prompt=("Read the catalog generation contract using read_context. Generate the complete 850-product JSON catalog using those rules, with count and source digest."
                if phase == "create" else "Which inventory catalog source did we discuss? Use the saved context, do not call tools."),
            tools=[ToolSpec("read_context", "Read the offline inventory catalog",
                           {"type": "object", "properties": {}}, read_context)],
            api_url="https://offline.invalid", token_provider=lambda: credential_canary, on_event=lambda _: None,
            timeout_seconds=None, runtime_path=runtime, http_transport=httpx.MockTransport(respond),
            behavior_boundary=AssistantModelBoundary(reserve, text_reply=True), instruction=True, session_files=fs,
            system_message="Offline persistence test. Private policy marker: "+private_system)
        native_compaction = []
    if phase == "compact":
        before=dict(fs.files)
        try:
            fs.snapshot()
        except Problem as error:
            assert error.body["error"]["code"]=="SDK_STATE_PRIVATE_MATERIAL"
            assert fs.files==before and (root/"create-reference.json").is_file()
            proof={"phase":phase,"python_pid":os.getpid(),"sdk_stop":result["sdk_stop"],
                "sdk_session_id":sid,"conversation_id":cid,"native_compaction":native_compaction,
                "checkpoint_status":"blocked_private_material","reference_published":False,
                "sdk_files_rewritten":False,"last_valid_checkpoint_retained":True,
                "runtime_log_level":"error",
                "offline_model_requests":len(calls),"tools":tools,"cloud_requests":0}
            (root/"compact-proof.json").write_text(json.dumps(proof,indent=2),encoding="utf-8")
            print(json.dumps(proof),flush=True)
            return
        raise AssertionError("raw compaction model snapshots must not be published")
    assert any(private_system in text for text in fs.files.values()),"real SDK did not persist the private canary before filtering"
    state = fs.snapshot()
    assert private_system not in state.encoded().decode()
    assert credential_canary not in state.encoded().decode()
    if phase != "create":
        for name, content in fs.files.items():
            if name.startswith("temp/"):
                assert state.files[name] == content
    raw, compressed = len(state.encoded()), len(zlib.compress(state.encoded()))
    if phase == "create":
        assert raw > 131072 and compressed > 16000,(raw,compressed)
        assert "checkpoints/index.md" in state.files, "SDK-owned checkpoint metadata was lost"
    assert tools == (["read_context"] if phase == "create" else []), "external tool replayed"
    ref = store.save(state, binding, conversation_id=cid)
    (root / (phase + "-reference.json")).write_text(ref.model_dump_json(), encoding="utf-8")
    proof = {"phase": phase, "python_pid": os.getpid(), "sdk_stop": result["sdk_stop"], "sdk_session_id": sid,
        "conversation_id": cid, "raw_bytes": raw, "zlib_bytes": compressed, "reference_bytes": len(ref.model_dump_json().encode()),
        "state_sha256": state.sha256, "files": {k: {"bytes": len(v.encode()), "sha256": hashlib.sha256(v.encode()).hexdigest()}
             for k, v in state.files.items()}, "offline_model_requests": len(calls), "tools": tools,
        "answer": result["final_message"], "native_compaction": native_compaction, "cloud_requests": 0}
    proof["private_system_filtered"]=True
    proof["transport_credential_absent"]=True
    (root / (phase + "-proof.json")).write_text(json.dumps(proof, indent=2), encoding="utf-8")
    print(json.dumps({**{k: v for k, v in proof.items() if k not in {"files","answer"}},
                      "answer_sha256":hashlib.sha256(proof["answer"].encode()).hexdigest()}), flush=True)


if __name__ == "__main__":
    root, phase, runtime = Path(sys.argv[1].removeprefix("\\\\?\\")), sys.argv[2], Path(sys.argv[3])
    root.mkdir(exist_ok=True)
    asyncio.run(execute(root, phase, runtime))
