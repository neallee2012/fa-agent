"""Free instructions keep raw input and SDK replies; legacy metrics bytes stay unchanged."""
import json

import pytest
from pathlib import Path
from test_behavior_assistant import assistant
from test_m3_auth import jwk_client, signing_key
from test_instruction_flow import instruction_models
from hosted_versions_stub import deployed_versions


@pytest.fixture
def runtime_path():
    # Admission-only tests cannot launch a runtime. SDK I/O has its own boundary test.
    return Path(__file__)


def test_free_input_preserves_original_text_and_legacy_submit_shape():
    from shared_context.behavior_assistant_wire import InstructionSubmit, AssistantSubmit
    text = "  請寫一首關於雨的短詩。\n不要計算數字。  "
    command = InstructionSubmit(request_id="free-one", content=text)
    assert command.content == text
    assert json.loads(command.model_dump_json()) == {
        "request_id": "free-one", "content": text, "authorize_own_result": True,
    }
    assert AssistantSubmit(request_id="old").model_dump_json() == (
        '{"request_id":"old","content":"Use the reviewed metrics to compute remaining items and save my result.",'
        '"authorize_own_result":true}')
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="blank", content=" \r\n\t ")


def test_free_admission_binds_original_task_and_does_not_repeat_unknown(assistant):
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit, Task
    from shared_context.errors import Problem
    h, control, human, _ = enabled(assistant)
    command = InstructionSubmit(request_id="free-original", content="  Explain why a rainbow is curved.\n")
    binding, status = control.submit(human, command)
    from shared_context.source_materials import digest
    assert digest(command.content) in binding.work.goal and status.state == "queued"
    task = Task.model_validate_json(h.runtime.job_input(binding))
    assert task.instruction.content == command.content and task.instruction.turn_number == 1
    writes = h.store._guard()
    assert control.submit(human, command)[0] == binding
    assert h.store._guard() == writes and h.endpoint.requests == []
    with pytest.raises(Problem):
        control.submit(human, command.model_copy(update={"content": "a different original intent"}))
    with pytest.raises(Problem):
        control.submit(human, InstructionSubmit(request_id="second-pending", content="Make that shorter.",
                        conversation_id=task.instruction.conversation_id))
    assert h.store._guard() == writes and h.endpoint.requests == []


def test_hosted_upgrade_pins_only_new_work_and_preserves_old_registration(assistant, monkeypatch):
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit
    h, control, human, _ = enabled(assistant)
    old, _ = control.submit(human, InstructionSubmit(request_id="old-version", content="Original work."))
    original = control.records[old.binding_id].model_dump_json()
    assert "hosted_agent_version" not in original
    deployed_versions(monkeypatch, "32")
    new, _ = control.submit(human, InstructionSubmit(request_id="new-version", content="New work."))
    assert control.records[new.binding_id].profile.instruction.hosted_agent_version == "32"
    deployed_versions(monkeypatch, "33")
    control.refresh()
    assert control.records[old.binding_id].model_dump_json() == original
    assert control.records[new.binding_id].profile.instruction.hosted_agent_version == "32"
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="client-version", content="No authority.", hosted_agent_version="33")


@pytest.mark.parametrize("answer", ["雨落窗沿，晚風把城市寫成一行詩。", "Use sorted(items, key=lambda x: x.name)."])
def test_free_provider_keeps_exact_sdk_stream_instead_of_metrics_only(answer):
    import asyncio
    import httpx
    from types import SimpleNamespace
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    raw = ("data: " + json.dumps({"id": "original-provider-id", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {"content": answer, "refusal": None}, "finish_reason": None}]},
        ensure_ascii=False) + '\n\ndata: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n').encode()
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=raw, headers={"content-type":"text/event-stream","x-private":"not-forwarded"})
    provider = AssistantProvider("https://offline.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _:SimpleNamespace(token="OFFLINE")),transport=httpx.MockTransport(respond))
    async def run():
        response = await provider.send({"model":"gpt-4.1-mini","messages":[{"role":"user","content":"自由指令"}],
                                      "stream":True,"max_tokens":1024},text_reply=True,model="gpt-4.1-mini")
        assert response.content == raw and "x-private" not in response.headers
        sdk_response = await AssistantModelBoundary(None,text_reply=True).response(response)
        assert sdk_response.content == raw
    asyncio.run(run())
    assert len(requests) == 1


@pytest.mark.parametrize("free", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
def test_production_provider_preserves_options_and_checks_current_work(monkeypatch, free, revoked):
    import asyncio
    from types import SimpleNamespace
    import httpx
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider, AssistantServer
    from shared_context.errors import Problem

    calls, gates, legacy = [], [], []
    model = "configured-second-model" if free else "gpt-4.1-mini"
    raw = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, content=raw, headers={"content-type": "text/event-stream"})
    def evidence(_):
        legacy.append(True)
        if free:
            raise AssertionError("instructions cannot inherit the old global expiry gate")
    monkeypatch.setattr(AssistantServer, "acquire_evidence", evidence)
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://offline.invalid/openai/v1/chat/completions")),
        runtime=SimpleNamespace(work_policy=SimpleNamespace(models=SimpleNamespace(
            select=lambda _: SimpleNamespace(supports_reasoning_effort=None)))))
    provider = _CurrentAssistantProvider(composition, SimpleNamespace(
        get_token=lambda _: SimpleNamespace(token="OFFLINE")), transport=httpx.MockTransport(respond))
    async def current():
        gates.append(True)
        if revoked:
            raise Problem(403, "ASSISTANT_EXECUTION_FENCED", "original work revoked")
    async def run():
        body = {"model": model, "messages": [{"role": "user", "content": "OFFLINE"}],
                "stream": True, **({} if free else {"max_tokens": 1024})}
        if revoked:
            with pytest.raises(Problem) as error:
                await provider.send(body, before_send=current, text_reply=free, model=model if free else None)
            assert error.value.body["error"]["code"] == "ASSISTANT_EXECUTION_FENCED"
        else:
            result = await provider.send(body, before_send=current, text_reply=free, model=model if free else None)
            if free:
                assert result.content == raw
            else:
                chunks = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: {")]
                assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    asyncio.run(run())
    assert gates == [True] and legacy == ([] if free else [True])
    assert len(calls) == (0 if revoked else 1)
    if calls:
        assert calls[0]["model"] == model
        if free:
            assert calls[0]["stream_options"]["include_usage"] is True
            assert "max_tokens" not in calls[0]


@pytest.mark.parametrize("supported", [False, True, None])
def test_production_provider_uses_declared_reasoning_support_without_changing_reserved_body(monkeypatch, supported):
    import asyncio
    import copy
    from types import SimpleNamespace
    from shared_context.behavior_assistant_server import _CurrentAssistantProvider
    from shared_context.behavior_assistant_gateway import AssistantProvider
    sent = []
    async def capture(self, body, **options):
        await options["before_send"]()
        sent.append(body)
    monkeypatch.setattr(AssistantProvider, "send", capture)
    composition = SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://offline.invalid/openai/v1/chat/completions")),
        runtime=SimpleNamespace(work_policy=SimpleNamespace(models=SimpleNamespace(
            select=lambda _: SimpleNamespace(supports_reasoning_effort=supported)))))
    provider = _CurrentAssistantProvider(composition, object())
    body = {"model": "configured-model", "messages": [{"role": "user", "content": "exact original"}],
            "reasoning_effort": "medium", "stream": True}
    original = copy.deepcopy(body)
    async def current():
        pass
    asyncio.run(provider.send(body, before_send=current, text_reply=True, model="configured-model"))
    assert body == original
    assert sent == [{k: v for k, v in original.items() if supported is not False or k != "reasoning_effort"}]


def test_new_instruction_is_not_a_six_call_two_dollar_timed_work(assistant):
    from datetime import timedelta
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_work_policy import WorkPreflightQuery,CostLedger
    from shared_context.behavior_operations import _row
    import hashlib
    h,control,human,_=enabled(assistant)
    original=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    h.time+=timedelta(hours=2)
    command=InstructionSubmit(request_id="untimed-original",content="Explain a rainbow.")
    preview=control.preflight(human,WorkPreflightQuery(read_id="new-read",request_id=command.request_id,
        submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest(),content=command.content))
    assert preview.decision=="allow" and preview.monetary_budget.required_microusd==0
    binding,status=control.submit(human,command)
    assert status.state=="queued"
    policy=control.records[binding.binding_id].runtime_policy
    assert policy.valid_until is None and policy.queue_deadline is None
    actual=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert actual.work_reservations_microusd==original.work_reservations_microusd
    assert actual.legacy_unresolved_reserve_microusd==original.legacy_unresolved_reserve_microusd


def test_independent_instructions_reserve_distinct_live_slots_without_touching_old_pool(assistant):
    from dataclasses import replace
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_workers import worker_key
    h,control,human,_=enabled(assistant)
    oldkey=worker_key("pool",h.workers.contract.workspace_id)
    original=h.store.read_snapshot((oldkey,))[oldkey]
    first,_=control.submit(human,InstructionSubmit(request_id="parallel-one",content="Write a poem."))
    second,_=control.submit(human,InstructionSubmit(request_id="parallel-two",content="Explain recursion."))
    starts=[]
    h.workers.adapter=replace(h.workers.adapter,start=lambda start:starts.append(start))
    h.workers.start(first.job_id,first.owner)
    h.workers.start(second.job_id,second.owner)
    assert len(starts)==2 and h.workers.inspect(first.job_id).state=="starting"
    assert h.workers.inspect(second.job_id).state=="starting"
    assert h.store.read_snapshot((oldkey,))[oldkey]==original
    assert h.workers.pool_key(first.job_id)!=h.workers.pool_key(second.job_id)
    assignment1,_=h.assistant.issue(starts[0])
    assignment2,_=h.assistant.issue(starts[1])
    assert assignment1.sdk_session_id!=assignment2.sdk_session_id
    assert assignment1.task.instruction.content=="Write a poem."
    assert assignment2.task.instruction.content=="Explain recursion."
    assert h.endpoint.requests==[]


def test_current_session_reads_each_registered_work_once_without_caching_authority_between_calls(assistant):
    from dataclasses import replace
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_work_policy import work_policy_key
    from shared_context.errors import Problem
    from shared_context.behavior_operations import control_key
    h, control, human, _ = enabled(assistant)
    binding, _ = control.submit(human, InstructionSubmit(request_id="read-once", content="Explain recursion."))
    unrelated, _ = control.submit(human, InstructionSubmit(request_id="unrelated-read", content="Independent work."))
    unrelated_key = work_policy_key("registration", control.records[unrelated.binding_id].admission_id)
    starts = []
    h.workers.adapter = replace(h.workers.adapter, start=lambda command: starts.append(command))
    h.workers.start(binding.job_id, binding.owner)
    assignment, ticket = h.assistant.issue(starts[0])
    principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers("agent")))
    registration_key = work_policy_key("registration", control.records[binding.binding_id].admission_id)
    h.transport.requests.clear()
    session = h.assistant.current_session(binding.binding_id, assignment.session_id, ticket, principal)
    assert session.assignment == assignment
    reads = [request for request in h.transport.requests
             if request.method == "GET" and registration_key in str(request.url)]
    assert len(reads) == 1
    assert not any(request.method == "GET" and unrelated_key in str(request.url)
                   for request in h.transport.requests)
    h.mutate(control_key("owner", binding.owner.owner_record_id), active=False)
    with pytest.raises(Problem):
        h.assistant.current_session(binding.binding_id, assignment.session_id, ticket, principal)


@pytest.mark.parametrize("revoke", [None, "registration", "owner"])
def test_instruction_effect_uses_its_transaction_snapshot_not_repeated_registration_reads(assistant,revoke):
    from dataclasses import replace
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit,Reservation
    from shared_context.behavior_work_policy import work_policy_key
    from shared_context.behavior_wire import Command
    from shared_context.behavior_operations import control_key
    from shared_context.errors import Problem
    h,control,human,_=enabled(assistant)
    binding,_=control.submit(human,InstructionSubmit(request_id="exact-effect-snapshot",content="Explain recursion."))
    starts=[]
    h.workers.adapter=replace(h.workers.adapter,start=lambda command:starts.append(command))
    h.workers.start(binding.job_id,binding.owner)
    assignment,ticket=h.assistant.issue(starts[0])
    principal=h.verifier(h.client.build_request("GET","/",headers=h.headers("agent")))
    h.assistant.acquire_current(binding.binding_id,assignment.session_id,ticket,Command(request_id="sdk-start"),principal)
    session=h.assistant.current_session(binding.binding_id,assignment.session_id,ticket,principal)
    registration=work_policy_key("registration",control.records[binding.binding_id].admission_id)
    if revoke=="registration":
        h.mutate(registration,submit_sha256="e"*64)
    elif revoke=="owner":
        h.mutate(control_key("owner",binding.owner.owner_record_id),active=False)
    h.transport.requests.clear()
    command=Reservation(request_id="a"*64,request_sha256="a"*64)
    if revoke:
        with pytest.raises(Problem):
            h.assistant.reserve_model(session,command,principal)
        assert not any(r.method=="POST" for r in h.transport.requests)
    else:
        assert h.assistant.reserve_model(session,command,principal).state=="accepted"
        reads=[r for r in h.transport.requests if r.method=="GET" and registration in str(r.url)]
        assert len(reads)==1


@pytest.mark.parametrize("budget_disabled", [False, True])
def test_seven_real_model_http_requests_keep_selected_deployment_and_original_money(assistant, budget_disabled):
    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_api import create_assistant_app
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.behavior_assistant_wire import InstructionSubmit,Reservation,ModelRequest
    from shared_context.behavior_wire import Command
    from shared_context.behavior_operations import _row
    from shared_context.behavior_work_policy import CostLedger
    from shared_context.source_materials import digest
    h,control,human,facts=enabled(assistant)
    if budget_disabled:
        from shared_context.behavior_work_budget import BudgetEnforcementCommand,set_budget_enforcement
        set_budget_enforcement(control,human,BudgetEnforcementCommand(
            request_id="approved-accounting-only",policy_sha256=control.policy.sha256,enforced=False))
    before=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    h.endpoint.expected_url="https://provider.invalid/openai/v1/chat/completions"
    h.endpoint.expected_model="configured-second-model"
    h.endpoint.unbounded=True
    h.endpoint.replies=iter([{"content":"OFFLINE response"}]*(8 if budget_disabled else 7))
    provider=AssistantProvider(h.endpoint.expected_url,SimpleNamespace(
        get_token=lambda _:SimpleNamespace(token="OFFLINE-not-a-credential")),transport=h.endpoint.transport)
    h.app=create_assistant_app(h.assistant,h.verifier,provider=provider)
    h.client=TestClient(h.app)
    binding,_=control.submit(human,InstructionSubmit(request_id="seven-requests",content="Explain recursion.",
                                                      model="configured-second-model"))
    assigned=[]
    def start(command):
        assignment,ticket=h.assistant.issue(command)
        assigned.append((assignment,ticket))
    h.workers.adapter=replace(h.workers.adapter,start=start)
    h.workers.start(binding.job_id,binding.owner)
    assignment,ticket=assigned[0]
    root=f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
    headers={**h.headers("agent"),"x-behavior-assistant-ticket":ticket.get_secret_value()}
    assert h.client.post(root+"acquire",content=Command(request_id="sdk-start").model_dump_json(),
                         headers=headers).status_code==200
    for index in range(7):
        if index==3:h.time+=timedelta(seconds=301)
        body={"model":"configured-second-model","messages":[{"role":"user","content":f"OFFLINE round {index}"}],
              "stream":True}
        request_hash=digest(body)
        reserved=h.client.post(root+"reserve-model",content=Reservation(request_id=request_hash,
            request_sha256=request_hash).model_dump_json(),headers=headers)
        assert reserved.status_code==200,reserved.text
        sent=ModelRequest(request_id=request_hash,body=body)
        response=h.client.post(root+"model",content=sent.model_dump_json(),headers=headers)
        assert response.status_code==200,response.text
    assert len(h.endpoint.requests)==7
    assert all(p["model"]=="configured-second-model" and "max_tokens" not in p and "max_completion_tokens" not in p
               for p in h.endpoint.requests)
    repeated=h.client.post(root+"model",content=sent.model_dump_json(),headers=headers)
    assert repeated.status_code==409 and len(h.endpoint.requests)==7
    after=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert after.work_reservations_microusd==before.work_reservations_microusd
    assert after.legacy_unresolved_reserve_microusd==before.legacy_unresolved_reserve_microusd
    assert after.model_pending_microusd==0 and after.model_metered_microusd==7*16
    from test_instruction_flow import instruction_inventory
    from shared_context.behavior_recovery_history import _Inventory
    from shared_context.behavior_instruction_cost import replay_cost_write
    inventory=instruction_inventory(h)
    cfg=SimpleNamespace(table=SimpleNamespace(scope=h.store._scope),sha256="f"*64,
                        workers=h.workers.contract,delivery=h.delivery.contract)
    view=_Inventory(inventory,cfg)
    ledger=before
    writes=sorted((r for r in view.receipts.values() if any(
        inventory.rows[k].payload["data"].get("kind")=="instruction_model_cost" for k,_ in r.writes)),
        key=lambda r:r.guard_revision)
    for receipt in writes:
        ledger=replay_cost_write(view,receipt,ledger,control.records,control.policy)
    assert ledger==after
    facts[0]=facts[0].model_copy(update={"observed_cumulative_microusd":99_000_000,"observed_at":h.time})
    blocked_body={**body,"messages":[{"role":"user","content":"Do not spend beyond the original shared total."}]}
    blocked_hash=digest(blocked_body)
    assert h.client.post(root+"reserve-model",content=Reservation(request_id=blocked_hash,
        request_sha256=blocked_hash).model_dump_json(),headers=headers).status_code==200
    refused=h.client.post(root+"model",content=ModelRequest(request_id=blocked_hash,body=blocked_body).model_dump_json(),headers=headers)
    assert refused.status_code==(200 if budget_disabled else 403),refused.text
    assert len(h.endpoint.requests)==(8 if budget_disabled else 7)
    updated=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert updated.observed_high_water_microusd==99_000_000
    assert updated.legacy_unresolved_reserve_microusd==before.legacy_unresolved_reserve_microusd


def test_public_progress_is_bound_and_pure_to_read(assistant):
    from dataclasses import replace
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit,ProgressUpdate,PublicProgress
    from shared_context.behavior_assistant_progress import public_event
    from shared_context.behavior_wire import Command
    h,control,human,_=enabled(assistant)
    binding,_=control.submit(human,InstructionSubmit(request_id="public-progress",content="Read the permitted source."))
    assigned=[]
    def start(command):assigned.append(h.assistant.issue(command))
    h.workers.adapter=replace(h.workers.adapter,start=start)
    h.workers.start(binding.job_id,binding.owner)
    assignment,ticket=assigned[0]
    headers={**h.headers("agent"),"x-behavior-assistant-ticket":ticket.get_secret_value()}
    root=f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
    assert h.client.post(root+"acquire",content=Command(request_id="sdk-start").model_dump_json(),headers=headers).status_code==200
    event=public_event({"type":"tool.execution_start","timestamp":"2026-01-01T00:00:00Z",
        "data":{"tool_name":"read_context","tool_call_id":"original","arguments":{"private":"NEVER_EXPORT"}}},1,{},
        allowed_tools=("read_context",))
    command=ProgressUpdate(request_id="progress-1",progress=PublicProgress(events=(event,)))
    stored=h.client.post(root+"progress",content=command.model_dump_json(),headers=headers)
    assert stored.status_code==200,stored.text
    guard=h.store._guard()
    result=control.conversation(human,assignment.task.instruction.conversation_id)
    assert result["turns"][0]["progress"]["events"][0]["tool_name"]=="read_context"
    assert result["turns"][0]["progress"]["reasoning_summary"]["availability"]=="not_provided"
    assert "NEVER_EXPORT" not in json.dumps(result) and h.store._guard()==guard


def test_api_startup_resumes_only_admitted_original_work_once(assistant):
    import asyncio
    from contextlib import ExitStack
    from dataclasses import replace
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit
    from shared_context.behavior_server import ServerComposition
    h,control,human,_=enabled(assistant)
    one,_=control.submit(human,InstructionSubmit(request_id="resume-one",content="Explain a rainbow."))
    two,_=control.submit(human,InstructionSubmit(request_id="resume-two",content="Write a short poem."))
    starts=[]
    h.workers.adapter=replace(h.workers.adapter,start=lambda command:starts.append(command))
    from types import SimpleNamespace
    # Startup also resumes queued peer messages for the configured targets (b156341e); this Agent has none queued.
    config=SimpleNamespace(targets=(SimpleNamespace(agent_id="00000001-1111-1111-1111-111111111111"),))
    composition=ServerComposition(config,h.runtime,h.verifier,None,(),ExitStack())
    async def run():
        composition.resume_instructions()
        composition.resume_instructions()
        for _ in range(100):
            if len(starts)==2:break
            await asyncio.sleep(.02)
        assert {start.binding.job_id for start in starts}=={one.job_id,two.job_id}
        assert len(starts)==2
        await composition.stop_instruction_observers()
        composition.close()
    asyncio.run(run())
    assert h.endpoint.requests==[]
    assert h.workers.inspect(one.job_id).generation==1 and h.workers.inspect(two.job_id).generation==1


def test_exact_instruction_storage_boundary_does_not_impose_old_character_cap(assistant):
    from test_work_policy_lifecycle import enabled
    from shared_context.behavior_assistant_wire import InstructionSubmit,Task
    h,control,human,_=enabled(assistant)
    command=InstructionSubmit(request_id="exact-8192",content="A"*8190)
    assert len(json.dumps(command.content,ensure_ascii=False).encode())==8192
    binding,_=control.submit(human,command)
    stored=Task.model_validate_json(h.runtime.job_input(binding))
    assert stored.instruction.content==command.content and len(h.runtime.job_input(binding))>8192
    with pytest.raises(ValueError):InstructionSubmit(request_id="too-large",content="A"*8191)
