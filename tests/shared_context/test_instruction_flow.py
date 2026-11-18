"""Real authority/HTTP/Table producers; pinned SDK I/O doubles, no executable or model."""
import asyncio
import base64
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

from test_behavior_assistant import assistant
from test_m3_auth import jwk_client, signing_key
from work_policy_lifecycle_helpers import enabled
from shared_context import m2_copilot
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_checkpoint_privacy import jsonl_lines
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.sdk_pin import RUNTIME_VERSION
from sdk_session_stub import OFFLINE_SESSION_RPC
from instruction_flow_helpers import instruction_models, sdk_io, runtime_path, instruction_inventory


def test_long_reference_get_and_original_ack_once(assistant,monkeypatch):
    import os
    from shared_context import behavior_wire as wire
    h,control,human,_=enabled(assistant)
    sdk_io(monkeypatch)
    text="OFFLINE完整長回答。\n"*1200
    h.endpoint.replies=iter([{"content":text}])
    h.endpoint.unbounded=True
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:None,require_current_native_evidence=lambda:None))
    command=InstructionSubmit(request_id="long-reference",content="Return the complete explanation.")
    binding,_=control.submit(human,command)
    done=h.runtime.controller.step(binding.binding_id)
    assert done.state=="succeeded" and done.result_forwarded
    cid=control.records[binding.binding_id].profile.instruction.conversation_id
    reply=h.client.get(f"/work-policy/v1/conversations/{cid}/turns/1/reply",headers=h.headers())
    assert reply.status_code==200 and reply.json()["text"]==text and len(reply.content)>8192
    path="/behavior/v1/bindings/"+binding.binding_id+"/delivery"
    pulled=h.client.post(path+"/pull",json={"request_id":"original-long-pull"},headers=h.headers())
    assert pulled.status_code==200,pulled.text
    reference=json.loads(base64.urlsafe_b64decode(pulled.json()["content"]))
    assert reference["sha256"]==hashlib.sha256(reply.content).hexdigest()
    assert reference["byte_length"]==len(reply.content)
    descriptor=wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pulled.headers["x-behavior-binding"]))
    acknowledgment=wire.PullAcknowledgment(request_id="original-long-ack",
        exchange_id=pulled.headers["x-behavior-exchange"],acknowledgment_token=pulled.headers["x-behavior-ack-token"],
        binding=descriptor)
    ack=h.client.post(path+"/acknowledge",content=acknowledgment.model_dump_json(),headers=h.headers())
    assert ack.status_code==200 and ack.json()["result"]["fulfillment"]=="fulfilled"
    guard=h.store._guard()
    assert h.client.post(path+"/acknowledge",content=acknowledgment.model_dump_json(),headers=h.headers()).status_code==200
    assert h.store._guard()==guard and len(h.endpoint.requests)==1
    if os.environ.get("FA_REFERENCE_CAPTURE"):
        Path(os.environ["FA_REFERENCE_CAPTURE"]).write_text(json.dumps({
            "evidence":"OFFLINE real HTTP/Table producers; SDK I/O double; no native/provider/model",
            "command":json.loads(command.model_dump_json()),"binding":descriptor.model_dump(mode="json"),
            "reply_json_utf8":reply.content.decode("utf-8"),"reply_headers":{
                k:reply.headers[k] for k in ("content-type","cache-control","x-content-sha256")},
            "pull_payload":pulled.json(),"reference":reference,"ack_result":ack.json(),
            "repeated_ack_wrote":False,"model_transport_requests":1,
        },ensure_ascii=False,indent=2),encoding="utf-8")


def test_unknown_checkpoint_upload_keeps_reply_but_never_restarts_empty(assistant,monkeypatch):
    from shared_context.errors import Problem
    h,control,human,_=enabled(assistant)
    calls=sdk_io(monkeypatch)
    def unavailable(*_):
        raise Problem(503,"ARTIFACT_UNKNOWN","OFFLINE checkpoint put outcome unknown")
    monkeypatch.setattr(h.assistant.artifacts.store,"put",unavailable)
    h.endpoint.unbounded=True
    h.endpoint.replies=iter([{"content":"Complete public answer retained even if SDK archive is unavailable."}])
    binding,_=control.submit(human,InstructionSubmit(request_id="archive-limit",content="Explain recursion."))
    result=h.runtime.controller.step(binding.binding_id)
    assert result.state=="succeeded" and result.result_forwarded and len(calls)==1
    cid=control.records[binding.binding_id].profile.instruction.conversation_id
    assert control.read_reply(human,cid,1).text==calls[0]["answer"]
    progress=control.conversation(human,cid)
    assert not progress["can_follow_up"] and progress["follow_up_blocker"]=="SDK_STATE_UPLOAD_UNKNOWN"
    guard=h.store._guard()
    with pytest.raises(Problem):
        control.submit(human,InstructionSubmit(request_id="no-empty-restart",content="Continue.",conversation_id=cid))
    assert h.store._guard()==guard and len(calls)==1


def test_missing_checkpoint_part_never_starts_an_empty_sdk_and_keeps_old_answer(assistant,monkeypatch):
    from shared_context.errors import Problem
    from shared_context.behavior_assistant import key
    from shared_context.behavior_instruction import TurnArchive
    from shared_context.behavior_operations import _row
    h,control,human,_=enabled(assistant)
    calls=sdk_io(monkeypatch)
    h.endpoint.unbounded=True
    h.endpoint.replies=iter([{"content":"Original answer is still readable."}])
    first,_=control.submit(human,InstructionSubmit(request_id="saved-before-loss",content="Explain."))
    assert h.runtime.controller.step(first.binding_id).state=="succeeded"
    cid=control.records[first.binding_id].profile.instruction.conversation_id
    archive_key=key("text-archive",first.job_id)
    saved=_row(h.store.read_snapshot((archive_key,)),archive_key,TurnArchive)
    ref=saved.archive
    part=ref.parts[0]
    del h.assistant.artifacts.store._versions[(first.team_id,ref.artifact_id,part.version_id)]
    second,_=control.submit(human,InstructionSubmit(request_id="cannot-restart-empty",content="Continue.",conversation_id=cid))
    with pytest.raises(Problem):
        h.runtime.controller.step(second.binding_id)
    assert len(calls)==1 and len(h.endpoint.requests)==1
    assert control.read_reply(human,cid,1).text=="Original answer is still readable."
    assert _row(h.store.read_snapshot((archive_key,)),archive_key,TurnArchive)==saved


def test_failed_instruction_has_failure_delivery_not_a_nonexistent_text_reply(assistant, monkeypatch):
    from shared_context.errors import Problem
    h, control, human, _ = enabled(assistant)
    def failure():
        raise RuntimeError("OFFLINE SDK turn failure")
    calls = sdk_io(monkeypatch, after_response=failure)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE incomplete turn"}])
    binding, _ = control.submit(human, InstructionSubmit(request_id="failed-original", content="Explain recursion."))
    status = h.runtime.controller.step(binding.binding_id)
    assert status.state == "failed" and status.result_forwarded and len(calls) == 1
    cid = control.records[binding.binding_id].profile.instruction.conversation_id
    projection = control.conversation(human, cid)
    assert projection["turns"][0]["state"] == "failed"
    assert projection["turns"][0]["reply_available"] is False
    assert projection["turns"][0]["delivery_id"] is not None
    assert projection["can_follow_up"] is True
    with pytest.raises(Problem) as absent:
        control.read_reply(human, cid, 1)
    assert absent.value.body["error"]["code"] == "INSTRUCTION_REPLY_NOT_READY"


def test_completion_notification_requires_original_saved_session_and_ticket(assistant, monkeypatch):
    from fastapi.testclient import TestClient
    from shared_context.behavior_assistant_api import create_assistant_app, TICKET_HEADER
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    issued = []
    original = h.assistant.issue
    def issue(start):
        value = original(start)
        issued.append(value)
        return value
    monkeypatch.setattr(h.assistant, "issue", issue)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE complete answer"}])
    binding, _ = control.submit(human, InstructionSubmit(request_id="notify-original", content="Explain recursion."))
    assert h.runtime.controller.step(binding.binding_id).state == "succeeded"
    assignment, ticket = issued[0]
    notified = []
    client = TestClient(create_assistant_app(h.assistant, h.verifier, on_result_ready=notified.append))
    path = f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
    headers = {**h.headers("agent"), TICKET_HEADER: ticket.get_secret_value()}
    assert client.post(path+"status", json={"request_id": "status"}, headers=headers).status_code == 200
    assert notified == []
    assert client.post(path+"execution-ready", json={"request_id": "execution-ready"},
                       headers={**headers, TICKET_HEADER: "0"*64}).status_code == 403
    assert client.post(path+"execution-ready", json={"request_id": "other"}, headers=headers).status_code == 422
    assert client.post(path+"execution-ready", json={"request_id": "execution-ready"},
                       headers={**h.headers(), TICKET_HEADER: ticket.get_secret_value()}).status_code == 403
    assert notified == []
    guard = h.store._guard()
    response = client.post(path+"execution-ready", json={"request_id": "execution-ready"}, headers=headers)
    assert response.status_code == 200, response.text
    assert notified == [binding.binding_id] and h.store._guard() == guard


def test_seven_turn_conversation_remains_readable_with_unrelated_registered_work(assistant, monkeypatch):
    h, control, human, _ = enabled(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": f"Saved answer {n}"} for n in range(1, 8)])
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    for n in range(40):
        control.submit(human, InstructionSubmit(request_id=f"unrelated-{n}", content="Independent pending work."))
    cid = None
    for n in range(1, 8):
        binding, _ = control.submit(human, InstructionSubmit(
            request_id=f"conversation-turn-{n}", content=f"Question {n}", conversation_id=cid))
        cid = control.records[binding.binding_id].profile.instruction.conversation_id
        result = h.runtime.controller.step(binding.binding_id)
        assert result.state == "succeeded" and result.result_forwarded
    guard = h.store._guard()
    response = h.client.get(f"/work-policy/v1/conversations/{cid}", headers=h.headers())
    assert response.status_code == 200, response.text
    assert len(response.json()["turns"]) == 7
    assert all(turn["reply_available"] for turn in response.json()["turns"])
    reply = h.client.get(f"/work-policy/v1/conversations/{cid}/turns/7/reply", headers=h.headers())
    assert reply.status_code == 200 and reply.json()["text"] == "Saved answer 7"
    assert h.store._guard() == guard and len(h.endpoint.requests) == 7
