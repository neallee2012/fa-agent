"""Current API/Table/Hosted producer loop; only SDK/runtime/model I/O is replaced."""
import copy
import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import A, TENANT
from test_m3_auth import signing_key, jwk_client
from test_web_test_console_browser_fixture import assistant_harness
from assistant_api_helpers import assistant_api, picker_api
from instruction_flow_helpers import instruction_models
from work_policy_lifecycle_helpers import enabled
from web_test_console_standing_helpers import configuration
from shared_context import behavior_assistant_wire as aw
from local_http_helpers import _LocalHttp
from shared_context.behavior_assistant_checkpoint import unpack
from shared_context.behavior_instruction_models import InstructionModels
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import ExecutorAdapter
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from web_test_console.adapter import parse_config, canonical, sha
from web_test_console.bridge import create_app
from web_test_console.instructions import InstructionConnection
from web_test_console.freeform import InstructionReply, ConversationProjection, AvailableModels
from web_test_console_frozen_interface_helpers import open_ui, post, complete


FIXTURES=Path(__file__).parent/"fixtures"/"web_test_console"


def test_sidebar_agent_is_derived_from_the_actual_bound_configuration(picker_api):
    c = picker_api
    state = c.browser.ui.get("/api/state").json()
    assert state["agents"] == [{"id": c.config.standing_policy.template.agent_oid,
                               "name": "FA 助理", "description": "目前配置的工作助理"}]
    original = state["agents"]
    new = c.browser.ui.post("/api/instructions/conversations/new", json={}, headers=c.browser.headers)
    assert new.status_code == 200
    assert new.json()["agents"] == original
    assert len(new.json()["composer"]["threads"]) > len(state["composer"]["threads"])


@pytest.fixture
def real_interface(auth,jwk_client,signing_key,tmp_path,monkeypatch):
    from shared_context.behavior_code_identity import code_sha256 as core_code_sha256
    current_core=core_code_sha256()
    import assistant_sdk_fixture
    monkeypatch.setattr(assistant_sdk_fixture,"TENANT",TENANT)
    h,control,human,facts=enabled(assistant_harness(auth,jwk_client,signing_key),after_global=True)
    catalog=[{"deployment":name,"provisioning":"Succeeded","capabilities":{"chatCompletion":"true"}}
             for name in ("gpt-4.1-mini","configured-second-model")]
    control._models=InstructionModels(catalog,{
        name:{"max_output_tokens":32768,"input_microusd_per_million":1000000,"output_microusd_per_million":1000000}
        for name in ("gpt-4.1-mini","configured-second-model")},"gpt-4.1-mini")
    install_work_policy_routes(h.app,SimpleNamespace(runtime=h.runtime,verifier=h.verifier,
        installation=lambda:control.installed(),require_current_native_evidence=lambda:h.delivery._readiness(),
        assistant=SimpleNamespace(authority=h.assistant)))
    local=_LocalHttp(h.client)
    c=SimpleNamespace(h=h,control=control,config=parse_config(json.dumps(configuration(h,control)).encode()),
        journal=tmp_path/"same.sqlite3",core=current_core,calls=[],sdk_calls=[],sdk_errors=[],evidence={},starts=[],errors=[],
        mutate=lambda req,res:res,lost=False,reply_text=None)
    c.connection=InstructionConnection(schema_version="fa-console-instructions-connection-v1",
        configuration_sha256=c.config.sha256,backend_core_sha256=current_core)
    def start(request):
        # The retired Hosted job lane has no executor left; a retained test must never reach one.
        c.starts.append(request)
        error=AssertionError("old Hosted job lane retired: no executor start")
        c.errors.append(error)
        raise error
    h.workers.adapter=ExecutorAdapter(start=start,query=lambda binding:control_key(
        "offline-ui-instruction-execution",binding.start_request_id),stop=lambda _:None,verify=c.evidence.__getitem__)

    def request(req):
        before=copy.deepcopy(h.transport.backing.rows)
        response=local.handle_request(req)
        c.calls.append({"method":req.method,"path":req.url.path,"status":response.status_code,
                        "body_sha256":sha(req.content)})
        if req.method=="GET" or req.url.path.endswith("/preflight"):
            assert h.transport.backing.rows==before,"read-only API changed actual fixture inventory"
        if c.lost and req.url.path=="/work-policy/v1/submit" and response.status_code==200:
            raise httpx.ReadTimeout("OFFLINE response lost after actual admission")
        return c.mutate(req,response)
    c.backend=httpx.Client(transport=httpx.MockTransport(request))
    c.browser=open_ui(c)
    yield c
    c.browser.ui.close();c.backend.close();h.client.close()


def test_frozen_examples_and_public_progress_dto_match_exact_not_private_shapes():
    root=Path(os.environ.get("FA_UI_INTERFACE_ART",FIXTURES))
    raw=(root/"interface-producer-examples.json").read_bytes()
    assert sha(raw)=="e3d361a654c40172fb14cc65b4886083999049abaec016697e77ca675aacdf9f"
    examples=json.loads(raw)
    command=aw.InstructionSubmit.model_validate_json(canonical(examples["first_command"]))
    assert sha(command.model_dump_json().encode())==examples["preflight"]["submit_body_sha256"]
    assert command.content.startswith("  ") and command.content.endswith("\n")
    for name in ("submit","second_submit","reconcile_present"):
        assert InstructionReply.model_validate_json(canonical(examples[name])).model=="gpt-4.1-mini"
    conversation=ConversationProjection.model_validate_json(canonical(examples["conversation"]))
    assert all(turn.progress.reasoning_summary.text is None for turn in conversation.turns)
    assert all(turn.progress.events==() for turn in conversation.turns)
    assert AvailableModels.model_validate_json(canonical(examples["models"])).default_model=="gpt-4.1-mini"
    reply=aw.TextReply.model_validate_json(canonical(examples["reply"]["body"]))
    assert sha(reply.model_dump_json().encode())==examples["reply"]["headers"]["x-content-sha256"]
    assert examples["reconcile_missing"]["status"]==503
    assert examples["reconcile_missing"]["body"]["error"]["code"]=="WORK_SUBMIT_UNKNOWN"
    event=aw.PublicProgressEvent.model_validate_json(json.dumps({
        "seq":1,"at":"2026-09-15T10:00:00Z","kind":"tool_started","tool_name":"read_context","status":"started",
        "summary":"OFFLINE DTO mapping only","source":{"source_id":"source-one","version":1,"sha256":"a"*64,"byte_length":1}}))
    assert event.source.source_id=="source-one"
    with pytest.raises(ValueError):
        aw.PublicProgressEvent.model_validate_json(json.dumps({
            "seq":2,"at":"2026-09-15T10:00:00Z","kind":"reasoning_delta","summary":"PRIVATE"}))

