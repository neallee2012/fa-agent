"""Frozen real producer replay at the client read seam; not an updated backend.

Only transport origin, exchange and challenge are explicit OFFLINE placeholders.
No real credential/challenge is in the SDK-provided immutable producer artifact.
"""
import base64
import json
import os
from pathlib import Path

import httpx
import pytest

from shared_context import behavior_assistant_wire as aw
from shared_context import behavior_wire as wire
from web_test_console.adapter import ConsoleError, PullAdapter, SteadyConsoleConfig, canonical, sha
from web_test_console.freeform import InstructionAdapter, checked_delivery_reference

FIXTURES=Path(__file__).parent/"fixtures"/"web_test_console"


def test_frozen_long_reply_reference_and_original_ack_client_seam():
    root=Path(os.environ.get("FA_UI_REFERENCE_ART",FIXTURES))
    producer_raw=(root/"producer.json").read_bytes()
    assert sha(producer_raw)=="661929bbb141d82dae0faa8b51dbf93b61734be1348c2c94e7b7f14ad3986c83"
    p=json.loads(producer_raw)
    reply_bytes=p["reply_json_utf8"].encode("utf-8")
    reply=aw.TextReply.model_validate_json(reply_bytes)
    assert len(reply_bytes)==32643 and len(reply_bytes)>8192
    assert sha(reply_bytes)==p["reply_headers"]["x-content-sha256"]==p["reference"]["sha256"]
    assert len(reply_bytes)==p["reference"]["byte_length"]
    payload=canonical(p["pull_payload"])
    assert sha(payload)==p["binding"]["payload_digest"]
    binding=wire.DeliveryBinding.model_validate_json(canonical(p["binding"]))
    cfg=SteadyConsoleConfig.model_validate_json(json.dumps({
        "schema_version":"fa-web-test-console-steady-v2","authorization_mode":"backend-per-work",
        "console_id":"OFFLINE-reference-replay","backend_origin":"https://reference.example",
        "tenant_id":binding.route.tenant_id,"human_oid":binding.route.recipient_id,
        "client_id":"11111111-1111-1111-1111-111111111111","audience":"api://offline-reference",
        "backend_configuration_sha256":"a"*64,"backend_core_sha256":"b"*64,
        "tasks":[{"task_id":"replay","label":"OFFLINE producer replay","binding_id":"offline-binding",
            "job_id":"offline-job","work_id":p["pull_payload"]["work_id"],"event_id":p["pull_payload"]["event_id"],
            "root_request_id":p["pull_payload"]["root_request_id"],"obligation_id":binding.obligation_id,
            "owner_record_id":p["pull_payload"]["responsibility_owner"],"route":binding.route.model_dump(mode="json"),
            "channel_version":binding.channel_version,"submit_request_id":"offline-submit","fetch_request_id":"offline-fetch",
            "ack_request_id":"offline-ack","allowed_actions":["status","fetch","acknowledge"],"max_payload_bytes":131072}]}))
    task=cfg.tasks[0];calls=[]
    challenge="OFFLINE-reference-challenge";exchange="OFFLINE-reference-exchange"
    def replay(req):
        calls.append((req.method,req.url.path,json.loads(req.content) if req.content else None))
        assert req.url.host=="reference.example"
        if req.url.path.endswith("/reply"):
            assert req.method=="GET"
            return httpx.Response(200,content=reply_bytes,headers=p["reply_headers"])
        if req.url.path.endswith("/delivery/pull"):
            return httpx.Response(200,content=payload,headers={
                "Content-Type":"application/json","X-Behavior-Exchange":exchange,"X-Behavior-Ack-Token":challenge,
                "X-Behavior-Binding":base64.urlsafe_b64encode(canonical(p["binding"])).decode(),
                "X-Behavior-Payload-SHA256":binding.payload_digest,"X-Behavior-Confirmation-Level":"client_received"})
        assert req.url.path.endswith("/acknowledge")
        sent=wire.PullAcknowledgment.model_validate_json(req.content)
        assert sent.request_id==task.ack_request_id and sent.binding==binding
        assert sent.exchange_id==exchange and sent.acknowledgment_token==challenge
        return httpx.Response(200,json=p["ack_result"])
    with httpx.Client(transport=httpx.MockTransport(replay)) as transport:
        reader=InstructionAdapter(cfg,transport,None)
        read,raw=reader.read_text_reply(reply.conversation_id,reply.turn_number,reply.sdk_session_id,"OFFLINE-no-token")
        assert raw==reply_bytes and read.text==reply.text
        original=PullAdapter(cfg,transport)
        delivered=original.fetch(task,"OFFLINE-no-token",binding.delivery_id)
        assert delivered.binding==binding
        checked_delivery_reference(delivered.parsed.content,raw)
        for change in ({"sha256":"f"*64},{"byte_length":len(raw)-1}):
            bad={**p["reference"],**change}
            with pytest.raises(ConsoleError):
                checked_delivery_reference(canonical(bad),raw)
        receipt=original.acknowledge(task,delivered,"OFFLINE-no-token")
        assert receipt==p["ack_result"]
    assert [v[0] for v in calls]==["GET","POST","POST"]
    assert calls[-1][2]["binding"]==p["binding"]
    assert p["repeated_ack_wrote"] is False
    if output:=os.environ.get("FA_UI_INTERFACE_RESULTS"):
        folder=Path(output);folder.mkdir(parents=True,exist_ok=True)
        (folder/"reference-read-seam.json").write_text(json.dumps({
            "actual_environment":"NOT_RUN","dependency":"Repo-frozen synthetic producer replay using current product DTOs",
            "producer_sha256":sha(producer_raw),"reply_bytes":len(raw),"reply_sha256":sha(raw),
            "text_unchanged":read.text==reply.text,"reference":p["reference"],"binding":p["binding"],
            "ack_result":receipt,"client_ack_posts":1,"published_producer_repeated_ack_wrote":False,
            "transport_exchange_challenge":"OFFLINE placeholders; not exported original secrets",
        },indent=2),encoding="utf-8")
