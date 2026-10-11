"""New native-mode producer/history seam only; all external execution is forbidden."""
from dataclasses import replace
from datetime import timedelta
import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from test_instruction_flow import instruction_models
from hosted_versions_stub import deployed_versions
from test_standing_target_native import (
    retained_handoff,installer,current_machine_evidence,stopped_original,
    original_teams_gap,original_finite_worker_contract,next_window,next_review,original_version,
    bounded_initial_control_quota,closed_v2,original_completed_nonempty_page,installed_empty,operator,
    exact_source_initialization,fixed_original_high_water,expired_claim,source_contract,closed_case,
    offline_socket_guard,original_captured_route_producer,closed_finished,sufficient_initial_budget,
    durable_result,native_acquire,current_rotation,two_rotations,current_cancelled,rotated,jwk_client,signing_key,
)

pytestmark=pytest.mark.parametrize("source_contract",["claim"],indirect=True)


@pytest.mark.parametrize("upgrade", [False, True])
def test_two_instruction_native_prepares_are_independent_and_history_still_reads(stopped_original,tmp_path,monkeypatch,upgrade):
    import subprocess
    from copilot import CopilotClient
    def denied(*args,**kwargs):pytest.fail("no runtime/provider/session/model may run")
    monkeypatch.setattr(subprocess,"Popen",denied)
    monkeypatch.setattr(CopilotClient,"start",denied)
    c,_,_,_=stopped_original
    install,_=installer(c,retained_handoff(stopped_original),tmp_path)
    plan=install.plan(c.principal,installation_id="OFFLINE-instruction-policy")
    approval=install.authorize(c.principal,plan,decision_id="OFFLINE-instruction",
                               expires_at=c.clock[0]+timedelta(minutes=4))
    install.apply(c.principal,plan,approval)
    install.source(c.principal,plan,approval)
    c.parent.installation()
    current_machine_evidence(c,install)
    control=c.runtime.work_policy
    version=str(int(c.runtime.dep.native_launches.targets[0].agent_version)+1) if upgrade else None
    if version is not None:
        deployed_versions(monkeypatch, version)
    first,_=control.submit(c.principal,InstructionSubmit(request_id="native-free-one",content="Write a short poem."))
    second,_=control.submit(c.principal,InstructionSubmit(request_id="native-free-two",content="Explain a rainbow."))
    c.parent.installation()
    native,workers=c.runtime.dep.native_launches,c.runtime.dep.workers
    prepared=[]
    workers.adapter=replace(workers.adapter,start=lambda request:prepared.append(native.prepare(request)))
    workers.start(first.job_id,first.owner)
    workers.start(second.job_id,second.owner)
    assert len(prepared)==2
    assert prepared[0][0].target.session_id!=prepared[1][0].target.session_id
    if version is not None:
        assert all(launch.target.agent_version==version for launch, _ in prepared)
    assert workers.pool_key(first.job_id)!=workers.pool_key(second.job_id)
    c.parent.installation()
    from shared_context.behavior_work_budget import BudgetEnforcementCommand,set_budget_enforcement
    set_budget_enforcement(control,c.principal,BudgetEnforcementCommand(
        request_id="OFFLINE-budget-accounting",policy_sha256=control.policy.sha256,enforced=False))
    c.parent.installation()
    for binding in (first,second):
        execution=workers.inspect(binding.job_id).binding
        workers.cancel(binding.job_id,binding.owner,expected_binding=execution)
        c.parent.installation()
        evidence=native.fence_unclaimed(execution)
        c.parent.installation()
        workers.observe(binding.job_id,evidence,binding.owner)
        c.parent.installation()
        workers.forward_result(binding.job_id,binding.owner,c.runtime.dep.delivery)
        c.parent.installation()
        assert workers.inspect(binding.job_id).state=="cancelled"
        assert workers.inspect(binding.job_id).result_forwarded
    print("PASS: two native-mode instruction registrations and exact independent prepares, no provider, current history readable")
