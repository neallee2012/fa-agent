"""Deterministic signed admission interleavings; no SDK, model or external network."""

from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_operations import control_key, _row
from shared_context.behavior_work_policy import CostLedger, WorkPolicyControl, derive_work_binding
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_attachments import draft, picker, submit as console_submit
from test_web_test_console_frozen_interface import open_ui
from test_work_cost_policy import test_original_scope_and_spent_high_water_cannot_regress as check_high_water
from test_work_policy_submit_compatibility import post, route
from web_test_console.adapter import canonical, sha


@pytest.fixture
def runtime_path():
    return Path(__file__)


def test_original_budget_scope_and_high_water_without_runtime(assistant):
    check_high_water(assistant)


def test_two_independent_admissions_survive_one_exact_cas_collision(assistant, monkeypatch):
    h, control, human, starts = route(assistant)
    delayed = InstructionSubmit(request_id="racing-second", content="Second conversation.")
    concurrent = InstructionSubmit(request_id="racing-first", content="First conversation.")
    _, binding = derive_work_binding(control.policy, delayed.request_id, instruction=delayed)
    admission_id = control_key("delivery-request", "admit", binding.event_id)
    reached, release = Event(), Event()
    submit = h.store._submit
    collisions = []

    def pause(operations, request):
        if request.request_id == admission_id and not collisions:
            collisions.append(request.request_id)
            reached.set()
            assert release.wait(20), "Other admission did not finish"
        return submit(operations, request)

    monkeypatch.setattr(h.store, "_submit", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, h, "submit", delayed)
        assert reached.wait(20), "Did not reach the admission CAS"
        try:
            first = post(h, "submit", concurrent)
            assert first.status_code == 200, first.text
        finally:
            release.set()
        second = future.result(timeout=20)
    original = post(h, "submit-reconcile", delayed)
    assert second.status_code == 200, (second.status_code, second.text,
                                      original.status_code, original.text)
    assert original.status_code == 200 and original.json()["binding_id"] == binding.binding_id
    assert len(starts) == 2 and not h.endpoint.requests
    ledger=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert ledger.reservation_count==0 and ledger.work_reservations_microusd==0


@pytest.mark.parametrize("exhausted",[False,True])
def test_native_final_cost_guard_reloads_before_retry_without_relaxing_budget(assistant,monkeypatch,exhausted):
    h,control,human,starts=route(assistant)
    migration=MigrationStore(h.store._client,h.store._scope)
    monkeypatch.setattr(h.delivery,"store",migration)
    initial=control.cost_reader()
    latest=[initial]
    monkeypatch.setattr(control,"cost_reader",lambda:latest[0])
    sampled=[]
    observation=control.admission_observation

    def change_after_sample(command):
        value=observation(command)
        sampled.append(value)
        if len(sampled)==1:
            latest[0]=value.model_copy(update={
                "observed_cumulative_microusd":110_000_000 if exhausted else value.observed_cumulative_microusd+1})
        return value

    # Only mode/config selection is synthetic; the real native final-send check
    # runs through MigrationStore and its actual Table transaction hook.
    guard=SimpleNamespace(
        runtime=SimpleNamespace(mode="native",_now=h.runtime._now),store=migration,
        _admission_record=control._admission_record,
        policy_key=control.policy_key,revocation_key=control.revocation_key,cost_key=control.cost_key,
        installed=lambda rows:control.installed(rows).model_copy(update={"evidence_mode":"native"}),
        cost_reader=lambda:latest[0].model_copy(update={"evidence_mode":"native"}))
    refused=[]

    def native_commit(command,observed,commit):
        try:
            return WorkPolicyControl.admission_commit(guard,command,
                observed.model_copy(update={"evidence_mode":"native"}),commit)
        except Problem as error:
            refused.append((error.status,error.body["error"]["code"]))
            raise

    monkeypatch.setattr(control,"admission_observation",change_after_sample)
    monkeypatch.setattr(control,"admission_commit",native_commit)
    command=InstructionSubmit(request_id="native-guard-synthetic",content="Original command.")
    response=post(h,"submit",command)
    assert refused[0]==(412,"WORK_POLICY_COST_CHANGED")
    assert len(sampled)==2
    assert response.status_code==(403 if exhausted else 200),response.text
    if exhausted:
        assert response.json()["error"]["code"]=="WORK_POLICY_CUMULATIVE_BUDGET_EXHAUSTED"
        assert post(h,"submit-reconcile",command).json()["state"]=="not_admitted"
        assert starts==[]
    else:
        assert len(starts)==1
    assert not h.endpoint.requests
    ledger=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert ledger.reservation_count==0 and ledger.model_pending_microusd==0


@pytest.mark.parametrize("fault",["412","before-loss","after-loss","enqueue"])
def test_original_incomplete_command_is_observed_then_manually_completed_once(assistant,monkeypatch,fault):
    h,control,human,starts=route(assistant)
    command=InstructionSubmit(request_id="original-interrupted",content="Keep exact bytes.\r\n")
    _,binding=derive_work_binding(control.policy,command.request_id,instruction=command)
    aid=control_key("delivery-request","admit",binding.event_id)
    real=h.store._submit
    rejected=[]

    def interrupted(operations,request):
        if request.request_id==aid and fault!="enqueue":
            rejected.append(request)
            if fault=="412":
                raise Problem(412,"BEHAVIOR_CONFLICT","Known rejected synthetic EGT")
            if fault=="after-loss":
                real(operations,request)
            raise Problem(503,"BEHAVIOR_COMMIT_UNKNOWN","Synthetic acknowledgment lost")
        if fault=="enqueue" and any(op[1]["RowKey"].startswith("behavior:v1:row:")
                and b'"state":"queued"' in op[1]["Payload"] and b'"spec":' in op[1]["Payload"] for op in operations):
            rejected.append(request)
            raise Problem(412,"BEHAVIOR_CONFLICT","Known rejected enqueue EGT")
        return real(operations,request)

    monkeypatch.setattr(h.store,"_submit",interrupted)
    failed=post(h,"submit",command)
    assert failed.status_code==(412 if fault in {"412","enqueue"} else 503),failed.text
    assert len(rejected)==(3 if fault=="412" else 1)
    assert starts==[]
    before=copy.deepcopy(h.transport.backing.rows)
    observed=post(h,"submit-reconcile",command)
    assert observed.status_code==200,observed.text
    assert observed.json()["request_id"]==command.request_id and observed.json()["retry_same_request"] is True
    assert observed.json()["state"]==("admission_incomplete" if fault in {"after-loss","enqueue"} else "not_admitted")
    assert h.transport.backing.rows==before
    if fault=="enqueue":
        with pytest.raises(Problem) as pending:
            h.runtime.submit(binding.binding_id,command,human,reconcile=True)
        assert pending.value.body["error"]["code"]=="BEHAVIOR_COMMIT_UNKNOWN"
        assert h.transport.backing.rows==before
    changed=post(h,"submit-reconcile",command.model_copy(update={"content":"Changed"}))
    assert changed.status_code==409
    monkeypatch.setattr(h.store,"_submit",real)
    retried=post(h,"submit",command)
    assert retried.status_code==200 and retried.json()["binding_id"]==binding.binding_id,retried.text
    preserved=copy.deepcopy(h.transport.backing.rows)
    assert post(h,"submit",command).content==retried.content
    assert post(h,"submit-reconcile",command).content==retried.content
    assert h.transport.backing.rows==preserved and not h.endpoint.requests
    ledger=_row(h.store.read_snapshot((control.cost_key,)),control.cost_key,CostLedger)
    assert ledger.reservation_count==0 and ledger.model_pending_microusd==0


def test_absent_request_is_current_read_not_a_permission_to_change_scope(assistant,monkeypatch):
    h,control,human,starts=route(assistant)
    command=InstructionSubmit(request_id="never-admitted",content="Original unsent command.")
    before=copy.deepcopy(h.transport.backing.rows)
    result=post(h,"submit-reconcile",command)
    assert result.status_code==200 and result.json()["state"]=="not_admitted",result.text
    assert h.transport.backing.rows==before and starts==[]
    control.revoke(human,"withdraw-original-read")
    assert post(h,"submit-reconcile",command).status_code==403
    assert h.client.post("/work-policy/v1/submit-reconcile",content=command.model_dump_json(),
                         headers=h.headers("agent")).status_code==403


def test_read_failure_cannot_claim_original_admission_absent(assistant,monkeypatch):
    h,control,_,_=route(assistant)
    command=InstructionSubmit(request_id="unknown-read",content="Original.")

    def unavailable(keys):
        raise Problem(503,"BEHAVIOR_READ_FAILED","Synthetic unavailable original store")

    monkeypatch.setattr(control.store,"read_snapshot",unavailable)
    response=post(h,"submit-reconcile",command)
    assert response.status_code==503 and "retry_same_request" not in response.text


def test_console_original_retry_requires_reconciliation_and_keeps_unknown_journal(picker,monkeypatch):
    c=picker
    original=draft(c)
    rid=original["request_id"]
    real=c.h.store._submit
    rejected=[]

    def reject_admission(operations,request):
        if any(b'"admitted_at":' in op[1]["Payload"] for op in operations):
            rejected.append(request)
            raise Problem(412,"BEHAVIOR_CONFLICT","Synthetic uncommitted admission")
        return real(operations,request)

    monkeypatch.setattr(c.h.store,"_submit",reject_admission)
    failed=console_submit(c,content="Keep full original command.\r\n")
    assert failed.status_code==409 and failed.json()["error"]["upstream_status"]==412
    console=c.browser.app.state.console
    old=copy.deepcopy(console.journal.get(rid,"submit"))
    command=console.instructions.command(rid).model_dump_json()
    path=f"/api/tasks/{rid}/submit-retry"
    denied=c.browser.ui.post(path,json={},headers=c.browser.headers)
    assert denied.status_code==409
    monkeypatch.setattr(c.h.store,"_submit",real)
    reconcile=c.browser.ui.post(f"/api/tasks/{rid}/submit-reconcile",json={},headers=c.browser.headers)
    assert reconcile.status_code==200,reconcile.text
    assert reconcile.json()["task"]["actions"]["submit-retry"]["enabled"]
    assert console.journal.get(rid,"submit")==old
    restarted=open_ui(c)
    try:
        c.calls.clear()
        retried=restarted.ui.post(path,json={},headers=restarted.headers)
        assert retried.status_code==200,retried.text
        task=retried.json()["task"]
        assert task["admission"]["conversation_id"]==old["intent"]["instruction_context"]["conversation_id"]
        assert task["submission"]["state"]=="UNKNOWN"
        assert not task["actions"]["submit-retry"]["enabled"]
        assert restarted.ui.post(path,json={},headers=restarted.headers).status_code==409
        assert c.calls==[("POST","/work-policy/v1/submit",canonical(old["intent"]["body"]))]
        assert console.journal.get(rid,"submit")==old
        assert console.instructions.command(rid).model_dump_json()==command
        assert restarted.app.state.console.instructions.admission(console.task(rid)) is not None
    finally:
        restarted.ui.close()


def test_console_retry_response_loss_reconciles_without_a_third_submit(picker,monkeypatch):
    c=picker
    rid=draft(c)["request_id"]
    real=c.h.store._submit

    def reject(operations,request):
        if any(b'"admitted_at":' in op[1]["Payload"] for op in operations):
            raise Problem(412,"BEHAVIOR_CONFLICT","Synthetic precommit rejection")
        return real(operations,request)

    monkeypatch.setattr(c.h.store,"_submit",reject)
    assert console_submit(c).status_code==409
    monkeypatch.setattr(c.h.store,"_submit",real)
    path=f"/api/tasks/{rid}"
    assert c.browser.ui.post(path+"/submit-reconcile",json={},headers=c.browser.headers).status_code==200
    old=c.browser.app.state.console.journal.get(rid,"submit")

    def lost(request,response):
        if request.url.path=="/work-policy/v1/submit" and response.status_code==200:
            raise httpx.ReadError("Synthetic committed response loss",request=request)
        return response

    c.mutate=lost
    retry=c.browser.ui.post(path+"/submit-retry",json={},headers=c.browser.headers)
    assert retry.status_code==503
    c.mutate=lambda request,response:response
    repeat=c.browser.ui.post(path+"/submit-retry",json={},headers=c.browser.headers)
    assert repeat.status_code==409
    result=c.browser.ui.post(path+"/submit-reconcile",json={},headers=c.browser.headers)
    assert result.status_code==200 and result.json()["task"]["admission"] is not None,result.text
    assert sum(route=="/work-policy/v1/submit" for _,route,_ in c.calls)==2
    assert c.browser.app.state.console.journal.get(rid,"submit")==old


def test_console_reconcile_mismatch_unknown_and_denial_never_enable_retry(picker):
    c=picker
    rid=draft(c)["request_id"]
    controller=c.browser.app.state.console.instructions
    # Only the original submit is lost; the new-conversation template read (fae25c09) answers normally.
    c.mutate=lambda request,response:(httpx.Response(503,json={"error":{"code":"WORK_SUBMIT_UNKNOWN"}})
                                      if request.url.path=="/work-policy/v1/submit" else response)
    assert console_submit(c).status_code==409
    body={"schema_version":"fa-work-submit-observation-v1","request_id":rid,
          "submit_sha256":sha(controller.command(rid).model_dump_json().encode("utf-8")),
          "state":"not_admitted","retry_same_request":True}
    path=f"/api/tasks/{rid}"
    journal=c.browser.app.state.console.journal
    original=copy.deepcopy(journal.get(rid,"submit"))
    responses=[
        httpx.Response(200,json={**body,"request_id":"other-request"}),
        httpx.Response(200,json={**body,"submit_sha256":"f"*64}),
        httpx.Response(200,json={**body,"retry_same_request":1}),
        httpx.Response(200,content=b'{"state":"not_admitted",'+canonical(body)[1:]),
        httpx.Response(503,json={"error":{"code":"WORK_SUBMIT_UNKNOWN"}}),
        httpx.Response(403,json={"error":{"code":"BEHAVIOR_ACCESS_DENIED"}}),
    ]
    for reply in responses:
        c.mutate=lambda request,response:reply
        observed=c.browser.ui.post(path+"/submit-reconcile",json={},headers=c.browser.headers)
        assert observed.status_code in {409,502}
        c.calls.clear()
        denied=c.browser.ui.post(path+"/submit-retry",json={},headers=c.browser.headers)
        assert denied.status_code==409 and c.calls==[]
        assert journal.get(rid,"submit")==original


def test_admission_semantic_sequence_conflict_is_not_retried(assistant,monkeypatch):
    h,_,_,starts=route(assistant)
    call=h.delivery._admit_once
    attempts=[]

    def wrong_sequence(command,context):
        attempts.append(True)
        return call(command.model_copy(update={"expected_sequence":1}),context)

    monkeypatch.setattr(h.delivery,"_admit_once",wrong_sequence)
    response=post(h,"submit",InstructionSubmit(request_id="wrong-original-sequence",content="Do not rebind."))
    assert response.status_code==409 and attempts==[True] and starts==[]


def test_partial_admission_without_receipt_is_unknown_not_retryable(assistant,monkeypatch):
    h,_,_,_=route(assistant)
    command=InstructionSubmit(request_id="missing-original-receipt",content="Preserve original material.")
    real=h.store._submit

    def interrupted(operations,request):
        value=real(operations,request)
        if any(b'"admitted_at":' in op[1]["Payload"] for op in operations):
            raise Problem(503,"BEHAVIOR_COMMIT_UNKNOWN","Synthetic lost commit")
        return value

    monkeypatch.setattr(h.store,"_submit",interrupted)
    assert post(h,"submit",command).status_code==503
    monkeypatch.setattr(h.store,"_receipt",lambda request:None)
    result=post(h,"submit-reconcile",command)
    assert result.status_code==503 and result.json()["error"]["code"]=="WORK_SUBMIT_UNKNOWN"
    assert "retry_same_request" not in result.text


def test_two_console_clients_cannot_repeat_one_reconciled_retry(picker,monkeypatch):
    c=picker
    rid=draft(c)["request_id"]
    real=c.h.store._submit

    def reject(operations,request):
        if any(b'"admitted_at":' in op[1]["Payload"] for op in operations):
            raise Problem(412,"BEHAVIOR_CONFLICT","Synthetic admission rejection")
        return real(operations,request)

    monkeypatch.setattr(c.h.store,"_submit",reject)
    assert console_submit(c,[c.attachment.model_dump(mode="json")]).status_code==409
    monkeypatch.setattr(c.h.store,"_submit",real)
    path=f"/api/tasks/{rid}"
    observed=c.browser.ui.post(path+"/submit-reconcile",json={},headers=c.browser.headers)
    assert observed.status_code==200
    other=open_ui(c)
    entered,release=Event(),Event()
    before=c.before

    def pause(request):
        if request.url.path=="/work-policy/v1/submit":
            entered.set()
            assert release.wait(20)

    c.before=pause
    c.calls.clear()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(c.browser.ui.post,path+"/submit-retry",json={},headers=c.browser.headers)
            assert entered.wait(20)
            try:
                refused=other.ui.post(path+"/submit-retry",json={},headers=other.headers)
                assert refused.status_code==409
            finally:
                release.set()
            response=future.result(timeout=20)
            assert response.status_code==200,response.text
        assert sum(route=="/work-policy/v1/submit" for _,route,_ in c.calls)==1
        sent=next(body for _,route,body in c.calls if route=="/work-policy/v1/submit")
        original=c.browser.app.state.console.journal.get(rid,"submit")
        assert sent==canonical(original["intent"]["body"])
        assert original["state"]=="UNKNOWN"
    finally:
        c.before=before
        release.set()
        other.ui.close()
