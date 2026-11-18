"""從 test_instruction_flow.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec
import httpx
import pytest
from copilot import CopilotClient
from copilot.client import GetStatusResponse
from copilot.session import CopilotSession
from shared_context import m2_copilot
from shared_context.behavior_checkpoint_privacy import jsonl_lines
from shared_context.sdk_pin import RUNTIME_VERSION
from sdk_session_stub import OFFLINE_SESSION_RPC


@pytest.fixture
def runtime_path():
    return Path(__file__)  # Only path validation; Popen is forbidden below.


@pytest.fixture(autouse=True)
def instruction_models(tmp_path,monkeypatch):
    catalog=tmp_path/"OFFLINE-models.json"
    facts=tmp_path/"OFFLINE-model-facts.json"
    names=("gpt-4.1-mini","configured-second-model")
    catalog.write_text(json.dumps([{"deployment":name,"provisioning":"Succeeded",
        "capabilities":{"chatCompletion":"true"}} for name in names]),encoding="utf-8")
    facts.write_text(json.dumps({name:{"max_output_tokens":32768,"input_microusd_per_million":400000,
        "output_microusd_per_million":1600000} for name in names}),encoding="utf-8")
    monkeypatch.setenv("FA_INSTRUCTION_MODEL_CATALOG_FILE",str(catalog))
    monkeypatch.setenv("FA_INSTRUCTION_MODEL_FACTS_FILE",str(facts))
    monkeypatch.setenv("FA_INSTRUCTION_DEFAULT_MODEL",names[0])


def sdk_io(monkeypatch,after_response=None,archive_oversize=False,*,expected_tools=("read_context","execute_operation")):
    calls = []
    def denied(*args, **kwargs):
        pytest.fail("no runtime process or real network is allowed")
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", denied)
    def factory(**connection):
        from threading import Event
        sdk = create_autospec(CopilotClient, instance=True)
        exited=Event()
        sdk._cli_process = SimpleNamespace(pid=999,poll=lambda:0 if exited.is_set() else None,
                                           wait=lambda:exited.wait(),returncode=0)
        sdk.stop.side_effect=lambda:exited.set()
        sdk.get_status.return_value = GetStatusResponse(version=RUNTIME_VERSION, protocol_version=2)
        async def opened(*args, **options):
            resume = bool(args)
            sid = args[0] if resume else options["session_id"]
            if resume:
                assert options["continue_pending_work"] is False
            assert {t.name for t in options["tools"]} == set(expected_tools)
            directory = Path(options["config_directory"]) / "session-state" / sid
            fs=options["create_session_fs_handler"](SimpleNamespace(session_id=sid)) if "create_session_fs_handler" in options else None
            if resume:
                raw=await fs.read_file("/session-state/events.jsonl") if fs is not None else (directory/"events.jsonl").read_text(encoding="utf-8")
                events = [json.loads(line) for line in jsonl_lines(raw)]
            else:
                events = [{"type":"session.start","data":{"sessionId":sid}}]
                metadata="X"*(128*1024+1) if archive_oversize else "OFFLINE SDK I/O metadata\n"
                if fs is not None:
                    await fs.write_file("/session-state/workspace.yaml",metadata)
                else:
                    directory.mkdir(parents=True)
                    (directory/"workspace.yaml").write_text(metadata,encoding="utf-8")
            session = create_autospec(CopilotSession, instance=True)
            session.rpc = OFFLINE_SESSION_RPC
            handlers=[]
            def subscribe(handler):
                handlers.append(handler)
                return lambda:handlers.remove(handler)
            session.on.side_effect=subscribe
            async def abort():
                from copilot.session_events import SessionIdleData
                for handler in handlers:handler(SimpleNamespace(data=SessionIdleData(aborted=True)))
            session.abort.side_effect=abort
            session.session_id = sid
            session.get_events.return_value = []
            async def send(prompt, **kwargs):
                messages = [{"role":"user" if event["type"]=="user.message" else "assistant",
                             "content":event["data"]["content"]}
                            for event in events if event["type"] in {"user.message","assistant.message"}]
                messages.append({"role":"user","content":prompt})
                response = await connection["request_handler"].send_request(
                    httpx.Request("POST","https://context.invalid/model/v1/chat/completions",
                                  json={"model":options["model"],"messages":messages,"stream":True}),
                    SimpleNamespace(request_id=f"offline-{len(calls)}",session_id=sid))
                await response.aread()
                data = [json.loads(line[6:]) for line in response.content.decode().splitlines()
                        if line.startswith("data: {")]
                answer = "".join(item["choices"][0]["delta"].get("content","") for item in data if item["choices"])
                calls.append({"resume":resume,"sdk_session_id":sid,"prompt":prompt,"messages":messages,"answer":answer})
                events.extend([{"type":"user.message","data":{"content":prompt}},
                               {"type":"assistant.message","data":{"content":answer}}])
                text="\n".join(json.dumps(e,ensure_ascii=False) for e in events)+"\n"
                if fs is not None:
                    await fs.write_file("/session-state/events.jsonl",text)
                else:
                    (directory/"events.jsonl").write_text(text,encoding="utf-8")
                if after_response is not None:
                    after_response()
                    await asyncio.sleep(0)
                    return "OFFLINE-cancelled-message"
                from copilot.session_events import AssistantMessageData,SessionIdleData
                final=SimpleNamespace(data=AssistantMessageData(content=answer,message_id="OFFLINE-reply"))
                for handler in handlers:
                    handler(final)
                    handler(SimpleNamespace(data=SessionIdleData()))
                return "OFFLINE-message-id"
            session.send_and_wait.side_effect = send
            session.send.side_effect = send
            return session
        sdk.create_session.side_effect = opened
        sdk.resume_session.side_effect = opened
        return sdk
    monkeypatch.setattr(m2_copilot,"CopilotClient",create_autospec(CopilotClient,side_effect=factory))
    return calls


def instruction_inventory(h):
    from shared_context.behavior_migration import AuthorityInventory
    from shared_context.records import Record
    from shared_context.behavior_store import GUARD_KEY
    rows,receipts,hashes={}, {}, {}
    for (_,_,key),value in h.transport.backing.rows.items():
        raw=value["Payload"]
        record=Record(json.loads(raw),value.metadata["etag"])
        hashes[key]=hashlib.sha256(raw).hexdigest()
        if key==GUARD_KEY:guard=record
        elif key.startswith("behavior:v1:row:"):rows[key.removeprefix("behavior:v1:row:")]=record
        elif key.startswith("behavior:v1:receipt:"):receipts[key.removeprefix("behavior:v1:receipt:")]=record
    return AuthorityInventory(h.store._scope,guard,rows,receipts,hashes)
