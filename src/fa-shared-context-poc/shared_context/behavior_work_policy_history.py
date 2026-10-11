"""Typed standing-policy/work/cost receipt verification; no new execution rights."""
from types import SimpleNamespace
from datetime import timedelta
from . import behavior_delivery as d,behavior_operations as op,behavior_workers as w
from .behavior_recovery_history import _Inventory
from .behavior_runtime import CommandPlan,RuntimePolicy,runtime_key,material,_PlanIndex
from .behavior_store import ControlRequest,_Row
from .behavior_work_policy import (
    WorkPolicyControl,StandingPolicyInstallation,WorkRegistration,CostLedger,
    WorkCostReservation,WorkDispatchPermission,PerWorkRuntimePolicy,WorkPolicyRevocation,CostObservationUpdate,
    derive_registered_work,registered_instruction,work_policy_key,cumulative_decision,
    registration_receipt_id,
)
from .behavior_runtime_rotation_history import require
from .behavior_runtime_transition import _payload_hash
from .behavior_foundry import NativeExecutionGrant,native_key
from .behavior_assistant_wire import Task
from .source_materials import digest
from .behavior_work_registry import inventory_registries, read_inventory_registry, registration_index_images
import hashlib


def _scope_registrations(view,config,p,all_records,environments):
    inventory=view.inventory
    policykey=work_policy_key("policy",*p.scope_ids)
    costkey=work_policy_key("cost",p.authority_domain)
    registrykey=work_policy_key("registry",*p.scope_ids)
    revocationkey=work_policy_key("revocation",*p.scope_ids)
    registry=read_inventory_registry(inventory,p)
    require(registry.policy_id==p.policy_id,"standing registry belongs to another policy")
    from .behavior_work_target import validate_target_history, target_key
    selection=validate_target_history(inventory,config) if p.agent_scope is None else None
    selection_key=target_key(p.authority_domain,p.agent_id)
    from .behavior_profile_selection import validate_profile_history,head_key,selection_key as profile_key
    profile_choices=validate_profile_history(inventory,config,p)
    from .memory_bridge_settings import (
        validate_memory_settings_history, validate_capture_record, captured_policy_fields, captured_memory_read_keys,
    )
    memory_choices=validate_memory_settings_history(inventory,config,p)
    records={}
    peer_roots={}
    from .behavior_conversation_transfer import matches_registration, history_ready
    for admission in registry.entries:
        key=work_policy_key("registration",admission)
        r=view.row(key,WorkRegistration,material=True)
        turn=r.profile.instruction
        peer_work=None if turn is None else turn.peer_work
        if r.routine is not None:
            from .behavior_routine import validate_submission
            require(peer_work is None,"a routine cannot replace a peer handoff")
            validate_submission(p,r.principal_oid,registered_instruction(r),r.routine)
        peer_root=None
        if peer_work is not None:
            from .behavior_agent_message_api import AgentCollaborationRoot
            from .behavior_agent_peer_work import validate_reference
            peer_root=view.row(peer_work.root_id,AgentCollaborationRoot,material=True)
            peer_roots[peer_work.root_id]=peer_root
            validate_reference(peer_root,p,peer_work,r.request_id,registered_instruction(r))
        transfer=environments.get(r.environment_transfer_id)
        require(r.environment_transfer_id is None or transfer is not None
                and history_ready(view,transfer) and matches_registration(transfer,r),
                "registered environment has no completed original transition")
        if r.hosted_session_id is not None:
            from .behavior_instruction import hosted_session_id
            require(turn is not None and (transfer is not None or r.agent_vm is not None
                    or r.hosted_session_id==hosted_session_id(turn.conversation_id)),
                    "Hosted session escaped its original conversation")
        profile_choice=None if r.profile_selection_id is None else profile_choices.get(r.profile_selection_id)
        require(r.profile_selection_id is None or profile_choice is not None,"original profile approval is missing")
        derived,binding,profile=derive_registered_work(p,r,"native",
            profile_template=None if profile_choice is None else profile_choice.command.template)
        require(r.admission_id==derived==admission and r.binding==binding and r.profile==profile
            and (turn is None or (None if turn.model_capacity is None else turn.model_capacity.profile)
                 == r.profile.model_capacity_profile)
            and r.policy_id==p.policy_id and r.policy_revision==p.revision and r.policy_sha256==p.sha256
            and r.agent_scope==p.agent_scope
            and r.principal_oid==(r.agent_origin.sender.worker.caller_oid if r.agent_origin is not None
                                  else config.identity.human_oid if peer_root is None
                                  else peer_root.directory.agents[0].principal_oid),
                "current work was not derived from the exact standing scope")
        rp=r.runtime_policy
        free=r.profile.instruction is not None
        capture=r.profile.memory_scope
        if capture is not None:
            require(capture.settings_id in memory_choices,"original memory setting approval is missing")
            validate_capture_record(p,capture,memory_choices[capture.settings_id])
        expected_policy=PerWorkRuntimePolicy(**{**p.legacy_template_policy.model_dump(),**captured_policy_fields(capture),
            "binding_digest":digest(["fa-behavior-api-v1","native",binding.model_dump(mode="json")]),
            "valid_until":None if free else rp.accepted_at+timedelta(seconds=p.queue_seconds),"policy_id":p.policy_id,
            "policy_revision":p.revision,"admission_id":admission,"job_id":binding.job_id,"stage":"queued",
            "accepted_at":rp.accepted_at,"queue_deadline":None if free else rp.accepted_at+timedelta(seconds=p.queue_seconds),
            "instruction":free})
        require(rp==expected_policy,
            "original work queue lifetime differs")
        if r.agent_origin is not None:
            from .behavior_agent_admission import validate_agent_admission_history
            validate_agent_admission_history(view,config,p,r,all_records)
            records[binding.binding_id]=r
            continue
        ownerkey=op.control_key("owner",binding.owner.owner_record_id)
        opkey=op.control_key("policy",binding.owner.policy_id)
        rpkey=runtime_key("policy",binding.binding_id);indexkey=runtime_key("plan-index",binding.binding_id)
        original_reads=(policykey,revocationkey,costkey,registrykey,key,ownerkey,opkey,rpkey,indexkey)
        from .behavior_instruction import conversation_key,registration_image
        conversation_reads=() if turn is None else (conversation_key(turn.conversation_id),)
        retained=view.receipts.get(key)
        profile_reads=(() if turn is None or turn.turn_number>1 else (head_key(p.authority_domain,p.agent_id),)) + (
            () if profile_choice is None else (profile_key(p.authority_domain,profile_choice.selection_id,p.agent_id),))
        if turn is not None and turn.capabilities is not None:
            from .behavior_capabilities import check_captured
            from .behavior_profile_selection import profile_from_template
            head_id=turn.capabilities.head_selection_id
            require(profile_choice is not None and (turn.turn_number>1 or head_id==profile_choice.selection_id)
                    and (head_id is None or head_id in profile_choices),"captured capabilities lost their selections")
            head_refs=() if head_id is None else profile_from_template(p,profile_choices[head_id].command.template).capabilities
            check_captured(r.profile.capabilities,turn.capabilities,head_refs)
            if turn.turn_number>1:
                profile_reads=tuple(dict.fromkeys((*profile_reads,head_key(p.authority_domain,p.agent_id),*(
                    () if head_id in (None,profile_choice.selection_id) else (profile_key(p.authority_domain,head_id,p.agent_id),)))))
        valid_reads={(*original_reads,*target_reads,*chosen_reads,*conversation_reads)
                     for target_reads in ((),(selection_key,))
                     for chosen_reads in (((),profile_reads) if profile_choice is None else (profile_reads,))}
        memory_reads=captured_memory_read_keys(p,capture,first_turn=free and turn.turn_number==1,records=all_records)
        valid_sets={frozenset((*base,*memory_reads)) for base in valid_reads}
        if capture is None:
            valid_sets.update(frozenset(base) for base in valid_reads)
        if peer_root is not None:
            from .behavior_agent_peer_work import peer_reads
            valid_sets={frozenset((*base,*peer_reads(peer_root))) for base in valid_sets}
        require(retained is not None and frozenset(retained.request.read_set) in valid_sets,
                "registration target predicate differs")
        if capture is not None:
            setting=memory_choices[capture.settings_id]
            require(view.receipts[setting.request.request_id].guard_revision<retained.guard_revision,
                    "work used memory settings before their human approval")
        if free and turn.turn_number==1 and peer_work is None:
            earlier=[value for value in memory_choices.values()
                     if view.receipts[value.request.request_id].guard_revision<retained.guard_revision]
            latest=max(earlier,key=lambda value:view.receipts[value.request.request_id].guard_revision) if earlier else None
            require((None if capture is None else capture.settings_id)==(None if latest is None else latest.settings_id),
                    "new conversation did not capture the current memory setting")
        if profile_choice is not None:
            require(view.receipts[profile_choice.request.request_id].guard_revision<retained.guard_revision,
                    "work used a profile before its human selection")
        if turn is not None and turn.turn_number==1 and peer_work is None:
            earlier=[value for value in profile_choices.values()
                     if view.receipts[value.request.request_id].guard_revision<retained.guard_revision]
            latest=max(earlier,key=lambda value:view.receipts[value.request.request_id].guard_revision) if earlier else None
            require(r.profile_selection_id==(None if latest is None else latest.selection_id),
                    "new conversation did not capture its current approved profile")
        future=selection is not None and admission not in selection.original_admissions
        if selection is not None:
            switched=view.receipts[selection.request.request_id]
            require((retained.guard_revision>switched.guard_revision)==future
                    and (not future or selection_key in retained.request.read_set),
                    "original/new work target selection ordering differs")
        from .behavior_work_policy import registration_request_digest
        request=ControlRequest(request_id=key,request_digest=registration_request_digest(r),
            read_set=retained.request.read_set)
        expected_owner=op.OwnerRecord(**binding.owner.model_dump(),active=True,operation_ids=(binding.operation_id,))
        expected_operation_policy=op.PolicyRecord(policy_id=binding.owner.policy_id,revision=rp.operation_policy_revision,active=True)
        require(view.row(ownerkey,op.OwnerRecord)==expected_owner
            and view.row(opkey,op.PolicyRecord)==expected_operation_policy,
            "original per-work owner/epoch/operation policy cannot be replaced")
        grantid=native_key("bootstrap-grant",binding.job_id);grantkey=native_key("grant",grantid)
        task=Task(job_id=binding.job_id,work_id=binding.work.work_id,definition_digest=digest(binding.work.model_dump(mode="json")),
            profile_digest=digest(profile.model_dump(mode="json")),source=profile.source,artifact_id=profile.artifact_id,
            artifact_version=profile.artifact_version,operation_id=binding.operation_id,instruction=profile.instruction)
        from .behavior_foundry import instruction_target
        from .behavior_team_registry import inventory_agent_scope
        targets=((inventory_agent_scope(inventory,config,p.agent_id).target,) if p.agent_scope is not None else
                 (selection.command.target,) if future else config.active_targets)
        grant=NativeExecutionGrant(grant_id=grantid,controller_id=config.controller_id,owner=binding.owner,
            policy_revision=rp.operation_policy_revision,job_id=binding.job_id,definition=binding.work,
            input_digest=hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
            targets=tuple(instruction_target(t,binding.job_id,agent_version=turn.hosted_agent_version,
                                            session_id=r.hosted_session_id)
                          for t in targets) if free else targets,
            active=True,remaining_uses=1,not_before=rp.accepted_at,expires_at=rp.queue_deadline,workload_version="assistant-v1.2")
        initial={
            key:view.hash(key),
            ownerkey:view.image_hash(ownerkey,expected_owner),
            opkey:view.image_hash(opkey,expected_operation_policy),
            rpkey:view.image_hash(rpkey,rp),indexkey:view.image_hash(indexkey,_PlanIndex()),
            grantkey:view.image_hash(grantkey,grant),
        }
        if turn is not None:
            ckey=conversation_key(turn.conversation_id)
            initial[ckey]=view.image_hash(ckey,registration_image(r,all_records,environment_transfers=environments))
        layout=registration_index_images(inventory,p,admission)
        exact={**initial,**{name:view.image_hash(name,image) for name,image in layout.items() if image is not None}}
        view.receipt(request,(*initial,*layout),exact=exact,
            result={"admission_id":admission,"binding_id":binding.binding_id,"not_yet_admitted":True})
        records[binding.binding_id]=r
    control=object.__new__(WorkPolicyControl)
    control.policy=p;control.records=records;control.profile_selections=profile_choices
    control.peer_roots=peer_roots
    return records,control


def validate_work_policy_history(inventory,config,history):
    from .behavior_agent_registry import AgentScopeRegistry,validate_agent_registry_history
    from .behavior_conversation_transfer import validate_history as validate_environments
    agent_history=validate_agent_registry_history(inventory,config)
    view=_Inventory(inventory,config)
    p=history.plan.policy
    policykey=work_policy_key("policy",p.authority_domain)
    costkey=work_policy_key("cost",p.authority_domain)
    installed=view.row(policykey,StandingPolicyInstallation,material=True)
    require(installed==history.installation and installed.policy==p
        and installed.operator_oid==config.identity.human_oid
        and installed.decision_id==history.approval.decision_id
        and history.approval.not_before<=installed.approved_at<history.approval.expires_at
        and installed.change_expires_at==history.approval.expires_at,
        "original standing approval changed")
    policies=[p,*(value.policy for value in agent_history.values() if isinstance(value,AgentScopeRegistry))]
    registries=inventory_registries(inventory,policies)
    all_records=[view.row(work_policy_key("registration",item),WorkRegistration,material=True)
                 for policy in policies
                 for item in registries[policy.sha256].entries]
    environments=validate_environments(view,all_records)
    records,controls={},{}
    for policy in policies:
        scoped,control=_scope_registrations(view,config,policy,all_records,environments)
        require(not set(records)&set(scoped),"agent scopes cannot share a work binding")
        records.update(scoped);controls[policy.agent_id]=control
    def record_control(record):
        return controls[None if record.agent_scope is None else record.agent_scope.agent_id]
    def receipt_control(receipt):
        matches=[control for control in controls.values() if control.policy_key in receipt.request.read_set]
        if matches:
            require(len(matches)==1,"cost receipt cannot use two agent policies")
            return matches[0]
        cost_records=[inventory.rows[key].payload["data"] for key,_ in receipt.writes
                      if inventory.rows[key].payload["data"].get("kind") in {"instruction_model_cost","instruction_turn_usage",
                                                                              "agent_turn_usage"}]
        require(len(cost_records)==1,"cost writer has no original policy scope")
        if cost_records[0]["kind"]=="agent_turn_usage":
            # ADR-0007 native turns have no job; the Agent of the original policy owns the record.
            agents=[control for control in controls.values()
                    if control.policy.template.agent_oid==cost_records[0]["agent_oid"]]
            require(len(agents)==1,"agent turn usage has no original Agent policy")
            return agents[0]
        owner=next((record for record in records.values() if record.binding.job_id==cost_records[0]["job_id"]),None)
        require(owner is not None,"model cost has no original agent work")
        return record_control(owner)
    ledger=history.initial_cost_ledger
    require(ledger.latest_observation_sha256==digest(installed.initial_observation.model_dump(mode="json"))
        and ledger.legacy_liability_sha256==history.plan.inventory_sha256
        and ledger.observed_high_water_microusd==installed.initial_observation.observed_cumulative_microusd
        and ledger.legacy_unresolved_reserve_microusd==installed.initial_observation.legacy_unresolved_reserve_microusd
        and ledger.latest_observed_at==installed.initial_observation.observed_at
        and digest(installed.initial_observation.model_dump(mode="json"))==history.plan.observations_sha256["cost"]
        and ledger.reservation_count==0 and ledger.work_reservations_microusd==0,
        "initial cumulative budget cannot replace or reset old exposure")
    initial_decision=cumulative_decision(p,ledger,installed.initial_observation,installed.approved_at,
                                        read_id="initial-cost",mode="native")
    require(initial_decision.reason in {"allowed","cumulative_budget_exhausted"},"initial cost scope/freshness/floor differs")
    cost_writers=sorted((r for r in view.receipts.values() if costkey in dict(r.writes)
        and r.guard_revision>history.plan.guard.revision+1),key=lambda r:r.guard_revision)
    registrations={r.admission_id:r for r in records.values()}
    admitted=set();dispatched=set()
    core_digest=digest({"contract":config.delivery.model_dump(mode="json"),"mode":"native"})
    for receipt in cost_writers:
        control=receipt_control(receipt);p=control.policy
        policykey,revocationkey=control.policy_key,control.revocation_key
        reservation=[r for r in registrations if work_policy_key("reservation",r) in dict(receipt.writes)]
        dispatch=[r for r in registrations if work_policy_key("dispatch",r) in dict(receipt.writes)]
        updates=[key for key,_ in receipt.writes if key!=costkey and inventory.rows[key].payload["data"].get("kind")=="steady_cost_observation_update"]
        model_updates=[key for key,_ in receipt.writes if inventory.rows[key].payload["data"].get("kind")=="instruction_model_cost"]
        turn_usage=[key for key,_ in receipt.writes if inventory.rows[key].payload["data"].get("kind")=="instruction_turn_usage"]
        agent_usage=[key for key,_ in receipt.writes if inventory.rows[key].payload["data"].get("kind")=="agent_turn_usage"]
        budget_changes=[key for key,_ in receipt.writes
                        if inventory.rows[key].payload["data"].get("kind")=="steady_budget_enforcement_change"]
        require(len(reservation)+len(dispatch)+len(updates)+len(model_updates)+len(turn_usage)+len(agent_usage)
                +len(budget_changes)==1,
                "unknown monetary writer")
        if budget_changes:
            from .behavior_work_budget import replay_budget_enforcement
            ledger=replay_budget_enforcement(view,receipt,ledger,p,budget_changes[0])
        elif agent_usage:
            from .behavior_instruction_cost import replay_agent_turn_usage
            ledger=replay_agent_turn_usage(view,receipt,ledger,p.template.agent_oid,costkey)
        elif turn_usage:
            from .behavior_instruction_cost import replay_turn_usage
            ledger=replay_turn_usage(view,receipt,ledger,records,costkey)
        elif model_updates:
            from .behavior_instruction_cost import replay_cost_write
            ledger=replay_cost_write(view,receipt,ledger,records,p)
        elif updates:
            key=updates[0];update=view.row(key,CostObservationUpdate,material=True);o=update.observation
            observed_sha=digest(o.model_dump(mode="json"))
            require(update.policy_id==p.policy_id and key==work_policy_key("cost-observation",p.authority_domain,observed_sha),
                    "original monetary observation scope differs")
            allowed=cumulative_decision(p,ledger,o,update.recorded_at,read_id="cost-observation",mode="native",
                                         instruction=update.instruction)
            require(allowed.reason in {"allowed","cumulative_budget_exhausted"},"cumulative monetary observation regressed")
            view.receipt(ControlRequest(request_id=key,request_digest=observed_sha,read_set=(policykey,revocationkey,costkey,key)),
                (costkey,key),exact={key:view.hash(key)},result={"observation_sha256":observed_sha,"reservation_added":False})
            ledger=ledger.model_copy(update={
                "observed_high_water_microusd":max(ledger.observed_high_water_microusd,o.observed_cumulative_microusd),
                "legacy_unresolved_reserve_microusd":max(ledger.legacy_unresolved_reserve_microusd,o.legacy_unresolved_reserve_microusd),
                "latest_observed_at":o.observed_at,"latest_observation_sha256":observed_sha})
        elif reservation:
            admission=reservation[0];r=registrations[admission];b=r.binding
            heldkey=work_policy_key("reservation",admission)
            held=view.row(heldkey,WorkCostReservation,material=True)
            require(admission not in admitted and held.policy_sha256==p.sha256 and held.policy_id==p.policy_id
                and held.admission_id==admission and held.job_id==b.job_id and held.root_budget_id==b.owner.root_budget_id
                and held.estimate_microusd==p.work_estimate_microusd,"original per-work money reservation differs")
            decision=cumulative_decision(p,ledger,held.observation,held.reserved_at,read_id=r.request_id,mode="native")
            require(decision.decision=="allow","original admission exceeded fresh cumulative allowance")
            commandplan=view.row(runtime_key("plan",b.binding_id,"admission",r.request_id),CommandPlan,material=True)
            command=d._parse(commandplan.data["admission"],d.AdmissionRequest)
            quotas=tuple(op.control_key("delivery-quota",k,v) for k,v in (
                ("team",config.identity.team_id),("entry",config.delivery.entry_id),("root",b.owner.root_budget_id)))
            builder=SimpleNamespace(_enabled=lambda:(None,config.delivery),configuration_digest=lambda:core_digest,
                                    _quota_keys=lambda _:quotas,work_policy=control)
            expected=d.DeliveryCore.request_for(builder,command,b.owner)
            intent=op.control_key("delivery-intent",op.control_key("delivery-intent-id",b.event_id))
            writes=(op.control_key("delivery-conversation",b.owner.conversation_id),op.control_key("delivery-event",b.event_id),
                op.control_key("delivery-work",b.work.work_id),op.control_key("delivery-work-state",b.obligation_id),
                op.control_key("delivery-obligation",b.obligation_id),intent,op.control_key("delivery-index",b.owner.root_budget_id),
                *quotas,costkey,heldkey)
            require(commandplan.request==expected and receipt.request==expected and command.work==b.work and command.route==b.route,
                    "cost must commit with the original durable admission")
            view.receipt(expected,writes,exact={heldkey:view.hash(heldkey)})
            ledger=ledger.model_copy(update={"observed_high_water_microusd":max(ledger.observed_high_water_microusd,held.observation.observed_cumulative_microusd),
                "legacy_unresolved_reserve_microusd":max(ledger.legacy_unresolved_reserve_microusd,held.observation.legacy_unresolved_reserve_microusd),
                "work_reservations_microusd":ledger.work_reservations_microusd+held.estimate_microusd,
                "reservation_count":ledger.reservation_count+1,"latest_observed_at":held.observation.observed_at,
                "latest_observation_sha256":digest(held.observation.model_dump(mode="json"))})
            admitted.add(admission)
        else:
            admission=dispatch[0];r=registrations[admission];b=r.binding
            key=work_policy_key("dispatch",admission)
            permission=view.row(key,WorkDispatchPermission,material=True)
            after=permission.policy_after
            expected_policy=PerWorkRuntimePolicy.model_validate({**r.runtime_policy.model_dump(),
                "stage":"dispatched","dispatched_at":after.dispatched_at,"valid_until":after.valid_until})
            require(admission in admitted and admission not in dispatched
                and permission.start.binding.job_id==b.job_id and permission.start.binding.owner==b.owner
                and permission.start.binding.generation==1 and permission.start.checkpoint is None
                and after.stage=="dispatched" and after.accepted_at==r.runtime_policy.accepted_at
                and after.queue_deadline==r.runtime_policy.queue_deadline and after.admission_id==admission and after==expected_policy,
                "one original slot acquisition fixes this work's deadline")
            observed=permission.observation
            decision=cumulative_decision(p,ledger,observed,after.dispatched_at,read_id=r.request_id,mode="native")
            require(decision.reason in {"allowed","cumulative_budget_exhausted"}
                and (not ledger.budget_enforced or
                     decision.observed_high_water_microusd+decision.reserved_microusd+p.safety_reserve_microusd<=p.cumulative_ceiling_microusd),
                "dispatch exceeded original reserved cumulative allowance")
            control.runtime=SimpleNamespace(mode="native")
            reads=(w.worker_key("pool",config.workers.workspace_id),w.worker_key("job",b.job_id),
                op.control_key("budget",b.owner.root_budget_id),op.control_key("owner",b.owner.owner_record_id),
                op.control_key("policy",b.owner.policy_id),*control.start_keys(b.job_id))
            builder=SimpleNamespace(configuration_digest=lambda:digest({"contract":config.workers.model_dump(mode="json"),"mode":"native"}))
            expected=w.WorkerCore._request(builder,"start",(b.job_id,"1"),permission.start.binding.model_dump(mode="json"),reads)
            require(permission.start.request==expected==receipt.request,"dispatch request/read set differs")
            grantkey=native_key("grant",native_key("bootstrap-grant",b.job_id))
            view.receipt(expected,(w.worker_key("job",b.job_id),w.worker_key("pool",config.workers.workspace_id),
                op.control_key("budget",b.owner.root_budget_id),w.worker_key("attempt",b.job_id,"1"),
                runtime_key("policy",b.binding_id),costkey,key,grantkey),
                exact={key:view.hash(key),runtime_key("policy",b.binding_id):view.image_hash(runtime_key("policy",b.binding_id),after)})
            ledger=ledger.model_copy(update={
                "observed_high_water_microusd":max(ledger.observed_high_water_microusd,observed.observed_cumulative_microusd),
                "legacy_unresolved_reserve_microusd":max(ledger.legacy_unresolved_reserve_microusd,observed.legacy_unresolved_reserve_microusd),
                "latest_observed_at":observed.observed_at,"latest_observation_sha256":digest(observed.model_dump(mode="json"))})
            dispatched.add(admission)
        require(dict(receipt.writes)[costkey]==view.image_hash(costkey,ledger),"cumulative ledger changed outside exact reservation/observation")
    require(view.row(costkey,CostLedger)==ledger,"current monetary high-water/reservations differ")
    for admission,r in registrations.items():
        control=record_control(r);p=control.policy
        if r.profile.instruction is not None:
            b=r.binding
            dispatchkey=work_policy_key("dispatch",admission)
            expected=r.runtime_policy
            if dispatchkey in inventory.rows:
                permission=view.row(dispatchkey,WorkDispatchPermission,material=True)
                expected=PerWorkRuntimePolicy.model_validate({**r.runtime_policy.model_dump(),
                    "stage":"dispatched","dispatched_at":permission.policy_after.dispatched_at})
                require(permission.policy_after==expected and permission.start.binding.owner==b.owner
                    and permission.start.binding.job_id==b.job_id
                    and permission.start.input==Task(job_id=b.job_id,work_id=b.work.work_id,
                        definition_digest=digest(b.work.model_dump(mode="json")),
                        profile_digest=digest(r.profile.model_dump(mode="json")),source=r.profile.source,
                        artifact_id=r.profile.artifact_id,artifact_version=r.profile.artifact_version,
                        operation_id=b.operation_id,instruction=r.profile.instruction).model_dump_json().encode(),
                    "instruction dispatch changed its original text, model or no-deadline policy")
                poolkey=w.worker_key("pool",config.workers.workspace_id,b.job_id)
                control.runtime=SimpleNamespace(mode="native")
                reads=(poolkey,w.worker_key("job",b.job_id),op.control_key("budget",b.owner.root_budget_id),
                       op.control_key("owner",b.owner.owner_record_id),op.control_key("policy",b.owner.policy_id),
                       *control.start_keys(b.job_id))
                builder=SimpleNamespace(configuration_digest=lambda:digest({"contract":config.workers.model_dump(mode="json"),"mode":"native"}))
                req=w.WorkerCore._request(builder,"start",(b.job_id,str(permission.start.binding.generation)),
                    permission.start.binding.model_dump(mode="json"),reads)
                require(req==permission.start.request,"instruction once-start request differs")
                view.receipt(req,(poolkey,w.worker_key("job",b.job_id),
                    w.worker_key("attempt",b.job_id,str(permission.start.binding.generation)),
                    runtime_key("policy",b.binding_id),dispatchkey,native_key("grant",native_key("bootstrap-grant",b.job_id))),
                    exact={dispatchkey:view.hash(dispatchkey)})
            require(view.row(runtime_key("policy",b.binding_id),PerWorkRuntimePolicy)==expected,
                    "instruction policy acquired a fabricated deadline")
            continue
        expected=(view.row(work_policy_key("dispatch",admission),WorkDispatchPermission,material=True).policy_after
                  if admission in dispatched else r.runtime_policy)
        require(view.row(runtime_key("policy",r.binding.binding_id),PerWorkRuntimePolicy)==expected,
                "a work deadline cannot be renewed or borrowed")
    for control in controls.values():
        p=control.policy;policykey,revocationkey=control.policy_key,control.revocation_key
        if revocationkey in inventory.rows:
            revoked=view.row(revocationkey,WorkPolicyRevocation,material=True)
            require(revoked.policy_sha256==p.sha256 and revoked.policy_id==p.policy_id and revoked.principal_oid==config.identity.human_oid,
                    "only exact original human revocation is accepted")
            revreceipt=view.receipt(ControlRequest(request_id=revocationkey,request_digest=digest(revoked.model_dump(mode="json")),
                read_set=(policykey,revocationkey)),(revocationkey,),exact={revocationkey:view.hash(revocationkey)},
                result={"policy_id":p.policy_id,"revoked":True,"reservations_refunded":False})
            # Recording spend that already happened (a turn's or a request's usage) is not a new admission
            # or dispatch: a turn running when the policy was revoked must still record its usage.
            def records_spend(r):
                writes=dict(r.writes)
                kinds={k:inventory.rows[k].payload["data"].get("kind") for k in writes if k!=costkey}
                from .behavior_assistant import _Session, key as assistant_key
                from .behavior_instruction_cost import ModelCost
                from .behavior_turn_receipts import TurnStartRecord, TurnTicketRecord, start_key, ticket_handle
                for key,kind in kinds.items():
                    if kind!="instruction_model_cost":
                        continue
                    cost=view.row(key,ModelCost)
                    if r.request.request_id!=assistant_key("model-send",cost.session_id,cost.request_sha256):
                        continue
                    session=view.row(assistant_key("session",cost.session_id),_Session)
                    if session.vm_turn_ticket_id is None:
                        continue
                    ticket=view.row(session.vm_turn_ticket_id,TurnTicketRecord)
                    start=view.row(start_key(session.vm_turn_ticket_id),TurnStartRecord)
                    record=next((v for v in records.values() if v.binding.job_id==cost.job_id),None)
                    if (record is not None and record.agent_vm==ticket.ticket.vm
                            and record_control(record) is control and ticket.started
                            and ticket.ticket.assignment==session.assignment
                            and start.start.ticket==ticket_handle(ticket.ticket)
                            and start.start.worker.vm==ticket.ticket.vm
                            and start.start.worker.caller_oid==ticket.ticket.agent_oid
                            and start.start.job_id==cost.job_id==session.execution.job_id
                            and view.receipts[registration_receipt_id(record)].guard_revision
                                <revreceipt.guard_revision
                            and view.receipts[start.request.request_id].guard_revision<r.guard_revision):
                        return True
                return (any(kind in {"instruction_turn_usage","agent_turn_usage"} for kind in kinds.values())
                        or any(kind=="instruction_model_cost"
                               and r.request.request_id==op.control_key("instruction-model-usage",k)
                               for k,kind in kinds.items()))
            require(all(r.guard_revision<revreceipt.guard_revision for r in cost_writers
                        if receipt_control(r) is control and not records_spend(r)),
                    "revoked standing policy cannot admit or dispatch another paid work")
    from .behavior_channels_migration import CatalogChunk
    from .behavior_assistant import _Session,_ModelIntent,key as assistant_key
    retained=set()
    for key,_ in history.catalog:
        chunk=view.row(key,CatalogChunk)
        retained.update(e.row_id for e in chunk.entries if e.kind=="row")
    model_keys=set()
    for record in records.values():
        p=record_control(record).policy
        b=record.binding
        start=w.worker_key("start",b.job_id,"1")
        sid=assistant_key("session-id",start)
        sessionkey=assistant_key("session",sid)
        if sessionkey not in inventory.rows:continue
        session=view.row(sessionkey,_Session)
        require(session.binding_id==b.binding_id and session.execution.job_id==b.job_id
            and session.execution.start_request_id==start and session.assignment.session_id==sid
            and session.assignment.task.profile_digest==digest(record.profile.model_dump(mode="json")),
            "current SDK session/model allowance escaped its original work")
        owned={key for key,row in inventory.rows.items()
            if isinstance(row.payload["data"].get("request_sha256"),str)
            and key==assistant_key("model-intent",sid,row.payload["data"]["request_sha256"])}
        require(session.model_requests==len(owned) and (record.profile.instruction is not None or len(owned)<=p.max_model_requests)
            and (not session.model_intent_ids or set(session.model_intent_ids)==owned),
            "complete current model intent inventory differs from its bounded count")
        for rid in owned:
            intent=view.row(rid,_ModelIntent)
            require(rid==assistant_key("model-intent",sid,intent.request_sha256),"current model intent differs")
            model_keys.add(rid)
    for key,row in inventory.rows.items():
        if key not in retained and "request_sha256" in row.payload["data"] and "consumed" in row.payload["data"]:
            require(key in model_keys,"orphan model intent cannot become current work or disappear from exposure")
    return tuple(record.binding for record in records.values())
