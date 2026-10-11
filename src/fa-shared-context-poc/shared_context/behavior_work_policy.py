"""Standing scope, per-work execution lifetime, and one cumulative cost authority."""
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime,timedelta
from typing import Annotated,Literal
from pydantic import Field,field_validator,model_validator

from .behavior_runtime import RuntimePolicy,RuntimeBinding
from .behavior_assistant import AssistantProfile
from .behavior_operations import _Model,_utc,control_key
from .behavior_store import Digest
from .models import Identifier
from .errors import Problem
from .source_materials import digest
from .behavior_auth import BehaviorPrincipal
from .behavior_operations import OwnerRecord,PolicyRecord,BudgetRecord,_row,_write,_parse
from .behavior_runtime import runtime_key,material,_PlanIndex
from .behavior_store import ControlRequest,ControlChanges,_READ_VIEW
from .behavior_workers import worker_key,JobRecord,ExecutorStart
from .behavior_assistant_wire import InstructionSubmit, InstructionTask, TEXT_CHECKS, ModelDeployment
from .behavior_instruction_prompt import PromptProfile
from .behavior_attachments import TextAttachments, validate_attachments
from .behavior_text_files import UserTextFiles
from .memory_bridge_settings import MemorySettingReference, memory_setting_reference
from .behavior_turn_ticket import TurnAuthorization
from .behavior_agent_worker import AgentVmBinding
from .behavior_agent_message_wire import PeerTeamReference
from .behavior_turn_wire import TurnRelayContext, TurnStartReceipt
from .behavior_routine_wire import DailyRoutineDefinition, RoutineOccurrence

Money=Annotated[int,Field(ge=0,le=2**63-1)]


def work_policy_key(kind,*ids):
    return control_key("steady-work-"+kind,*ids)


class AgentScopeReference(_Model):
    schema_version:Literal["fa-agent-scope-reference-v1"]="fa-agent-scope-reference-v1"
    registry_id:Identifier
    agent_id:Identifier


class SteadyWorkPolicy(_Model):
    schema_version:Literal["fa-steady-work-policy-v1"]="fa-steady-work-policy-v1"
    policy_id:Identifier
    revision:Annotated[int,Field(ge=1)]
    configuration_sha256:Digest
    evidence_mode:Literal["native","offline_contract"]
    authority_domain:Identifier
    control_binding:Digest
    active:bool
    read_enabled:bool
    template:RuntimeBinding
    profile:AssistantProfile
    legacy_template_policy:RuntimePolicy
    cost_scope_sha256:Digest
    cost_observer_oid:Identifier
    cumulative_ceiling_microusd:Literal[110_000_000]=110_000_000
    safety_reserve_microusd:Literal[10_000_000]=10_000_000
    minimum_observed_microusd:Annotated[int,Field(ge=0,le=2**63-1)]
    work_estimate_microusd:Annotated[int,Field(gt=0,le=100_000_000)]
    cost_max_age_seconds:Annotated[int,Field(ge=1,le=3600)]
    queue_seconds:Annotated[int,Field(ge=1,le=86400)]
    execution_seconds:Literal[240]=240
    max_model_requests:Annotated[int,Field(ge=1,le=6)]
    max_generations:Literal[1]=1
    agent_scope:AgentScopeReference|None=Field(default=None,exclude_if=lambda value:value is None)
    routines:tuple[DailyRoutineDefinition,...]=Field(default=(),exclude_if=lambda value:not value)

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))

    @property
    def agent_id(self):
        return None if self.agent_scope is None else self.agent_scope.agent_id

    @property
    def scope_ids(self):
        return (self.authority_domain,) if self.agent_scope is None else (self.authority_domain,self.agent_id)

    @model_validator(mode="after")
    def scope(self):
        if (self.template.route.channel_id!="authenticated-pull" or self.template.child_jobs
                or self.profile.binding_id!=self.template.binding_id
                or self.max_model_requests>self.profile.max_model_requests
                or self.legacy_template_policy.binding_digest!=digest([
                    "fa-behavior-api-v1",self.evidence_mode,self.template.model_dump(mode="json")])):
            raise ValueError("exact fixed source/profile/recipient and original root scope required")
        return self


class CostObservation(_Model):
    schema_version:Literal["fa-cumulative-cost-observation-v1"]="fa-cumulative-cost-observation-v1"
    evidence_id:Identifier
    evidence_mode:Literal["native","offline_contract"]
    observer_oid:Identifier
    authority_domain:Identifier
    control_binding:Digest
    cost_scope_sha256:Digest
    unit:Literal["USD-micro"]="USD-micro"
    observed_cumulative_microusd:Money
    legacy_unresolved_reserve_microusd:Money
    legacy_liability_sha256:Digest
    evidence_sha256:Digest
    observed_at:datetime
    _time=field_validator("observed_at")(_utc)


class CostLedger(_Model):
    kind:Literal["steady_cumulative_cost_ledger"]="steady_cumulative_cost_ledger"
    authority_domain:Identifier
    cost_scope_sha256:Digest
    unit:Literal["USD-micro"]="USD-micro"
    observed_high_water_microusd:Money
    legacy_unresolved_reserve_microusd:Money
    legacy_liability_sha256:Digest
    work_reservations_microusd:Money=0
    reservation_count:Annotated[int,Field(ge=0)]=0
    latest_observed_at:datetime
    latest_observation_sha256:Digest
    model_pending_microusd:Money=Field(default=0,exclude_if=lambda v:v==0)
    model_metered_microusd:Money=Field(default=0,exclude_if=lambda v:v==0)
    # Sent turn model requests whose amount is unknown (ADR-0006 §5 A2); a count, never a guessed amount.
    model_unmetered_requests:Annotated[int,Field(ge=0)]=Field(default=0,exclude_if=lambda v:v==0)
    # ADR-0007 native turns that ended without a usage report; a count, never a guessed amount.
    model_unknown_turns:Annotated[int,Field(ge=0)]=Field(default=0,exclude_if=lambda v:v==0)
    budget_enforced:bool=Field(default=True,exclude_if=lambda v:v)
    _time=field_validator("latest_observed_at")(_utc)


class WorkCostReservation(_Model):
    kind:Literal["steady_work_cost_reservation"]="steady_work_cost_reservation"
    policy_id:Identifier
    policy_sha256:Digest
    admission_id:Identifier
    job_id:Identifier
    root_budget_id:Identifier
    estimate_microusd:Money
    observation:CostObservation
    reserved_at:datetime
    _time=field_validator("reserved_at")(_utc)


class CostObservationUpdate(_Model):
    kind:Literal["steady_cost_observation_update"]="steady_cost_observation_update"
    policy_id:Identifier
    observation:CostObservation
    recorded_at:datetime
    instruction:bool=Field(default=False,exclude_if=lambda v:not v)
    _time=field_validator("recorded_at")(_utc)


class PerWorkRuntimePolicy(RuntimePolicy):
    schema_version:Literal["fa-per-work-runtime-policy-v1"]="fa-per-work-runtime-policy-v1"
    policy_id:Identifier
    policy_revision:Annotated[int,Field(ge=1)]
    admission_id:Identifier
    job_id:Identifier
    stage:Literal["queued","dispatched"]
    accepted_at:datetime
    queue_deadline:datetime|None
    dispatched_at:datetime|None=None
    instruction:bool=Field(default=False,exclude_if=lambda value:not value)
    _times=field_validator("accepted_at","queue_deadline")(lambda value:None if value is None else _utc(value))

    @model_validator(mode="after")
    def work_deadline(self):
        if self.instruction:
            if self.valid_until is not None or self.queue_deadline is not None:
                raise ValueError("instructions have no artificial queue or execution deadline")
            if (self.stage=="queued") != (self.dispatched_at is None):
                raise ValueError("instruction dispatch state differs")
            return self
        if self.stage=="queued":
            if self.dispatched_at is not None or not self.accepted_at<self.valid_until==self.queue_deadline:
                raise ValueError("queued work has only its own finite queue permission")
        elif (self.dispatched_at is None or not self.accepted_at<=self.dispatched_at<self.queue_deadline
              or self.valid_until!=_utc(self.dispatched_at)+timedelta(seconds=240)):
            raise ValueError("dispatch fixes this work's unchanged 240-second execution permission")
        return self


class WorkDispatchPermission(_Model):
    kind:Literal["steady_work_dispatch_permission"]="steady_work_dispatch_permission"
    admission_id:Identifier
    start:ExecutorStart
    policy_after:PerWorkRuntimePolicy
    observation:CostObservation


class WorkPolicyDecision(_Model):
    schema_version:Literal["fa-work-policy-decision-v1"]="fa-work-policy-decision-v1"
    read_id:Identifier
    policy_id:Identifier
    policy_revision:Annotated[int,Field(ge=1)]
    decision:Literal["allow","deny"]
    reason:Identifier
    observed_at:datetime
    budget_id:Identifier
    unit:Literal["USD-micro"]="USD-micro"
    remaining_microusd:Money|None
    required_microusd:Money
    observed_high_water_microusd:Money
    reserved_microusd:Money
    safety_reserve_microusd:Literal[10_000_000]|None=10_000_000
    cumulative_ceiling_microusd:Literal[110_000_000]|None=110_000_000
    budget_enforced:bool=Field(default=True,exclude_if=lambda v:v)
    authorization:Literal[False]=False
    provider_hard_cap:Literal[False]=False
    _time=field_validator("observed_at")(_utc)


class WorkSubmitObservation(_Model):
    schema_version:Literal["fa-work-submit-observation-v1"]
    request_id:Identifier
    submit_sha256:Digest
    state:Literal["not_admitted","admission_incomplete"]
    retry_same_request:Literal[True]


class WorkPreflightQuery(_Model):
    read_id:Identifier
    request_id:Identifier
    submit_body_sha256:Digest
    content:str|None=Field(default=None,exclude_if=lambda v:v is None)
    conversation_id:Identifier|None=Field(default=None,exclude_if=lambda v:v is None)
    model:ModelDeployment|None=Field(default=None,exclude_if=lambda v:v is None)
    attachments:TextAttachments=Field(default=(),exclude_if=lambda v:not v)
    files:UserTextFiles=Field(default=(),exclude_if=lambda v:not v)

    _attachments=field_validator("attachments")(validate_attachments)

    @model_validator(mode="after")
    def original_input(self):
        if self.content is None:
            if self.files or self.attachments:
                raise ValueError("Attachments require the original instruction text")
        else:
            InstructionSubmit(request_id=self.request_id,content=self.content,
                conversation_id=self.conversation_id,model=self.model,attachments=self.attachments,files=self.files)
        return self


class WorkPreflight(_Model):
    schema_version:Literal["fa-steady-work-preflight-v1"]="fa-steady-work-preflight-v1"
    read_id:Identifier
    request_id:Identifier
    submit_body_sha256:Digest
    configuration_sha256:Digest
    policy_sha256:Digest
    tenant_id:Identifier
    team_id:Identifier
    human_oid:Identifier
    template_binding_id:Identifier
    binding:RuntimeBinding
    decision:Literal["allow","deny"]
    reason:Identifier
    observed_at:datetime
    guard_revision:Annotated[int,Field(ge=0)]
    execution_deadline:datetime|None
    queue_deadline:datetime|None
    already_admitted:bool
    capacity_available:bool
    monetary_budget:WorkPolicyDecision
    abstract_attempts_remaining:Annotated[int,Field(ge=0)]
    abstract_units_remaining:Annotated[int,Field(ge=0)]
    authorization:Literal[False]=False
    conversation_id:Identifier|None=Field(default=None,exclude_if=lambda v:v is None)
    turn_number:int|None=Field(default=None,exclude_if=lambda v:v is None)
    model:ModelDeployment|None=Field(default=None,exclude_if=lambda v:v is None)
    memory_setting_ref:MemorySettingReference|None=Field(default=None,exclude_if=lambda v:v is None)
    _time=field_validator("observed_at")(_utc)


def cumulative_decision(policy,ledger,observation,now,*,read_id,mode,instruction=False):
    """Pure read; no reservation, receipt, model call, or dispatch permission."""
    now=_utc(now)
    scope_ok=(type(observation) is CostObservation and observation.evidence_mode==mode==policy.evidence_mode
              and observation.observer_oid==policy.cost_observer_oid and observation.authority_domain==policy.authority_domain
              and observation.control_binding==policy.control_binding and observation.cost_scope_sha256==policy.cost_scope_sha256
              and observation.legacy_liability_sha256==ledger.legacy_liability_sha256)
    fresh=scope_ok and observation.observed_at<=now and (instruction or not ledger.budget_enforced or
        now<observation.observed_at+timedelta(seconds=policy.cost_max_age_seconds))
    monotonic=scope_ok and (observation.observed_at>=ledger.latest_observed_at
              and observation.observed_cumulative_microusd>=ledger.observed_high_water_microusd
              and observation.observed_cumulative_microusd>=policy.minimum_observed_microusd
              and observation.legacy_unresolved_reserve_microusd>=ledger.legacy_unresolved_reserve_microusd)
    reason="allowed"
    if not policy.active:reason="policy_revoked"
    elif not policy.read_enabled:reason="result_access_revoked"
    elif not scope_ok:
        reason="cost_observation_scope"
    elif not fresh:
        reason="cost_observation_stale"
    elif not monotonic:
        reason="cost_observation_regressed"
    if ledger.authority_domain!=policy.authority_domain or ledger.cost_scope_sha256!=policy.cost_scope_sha256:
        raise Problem(409,"WORK_POLICY_BUDGET_SCOPE","the original cumulative cost authority differs")
    matched=fresh and monotonic
    high=max(ledger.observed_high_water_microusd,observation.observed_cumulative_microusd if matched else 0)
    legacy=max(ledger.legacy_unresolved_reserve_microusd,observation.legacy_unresolved_reserve_microusd if matched else 0)
    held=legacy+ledger.work_reservations_microusd+ledger.model_pending_microusd+ledger.model_metered_microusd
    remaining=max(0,policy.cumulative_ceiling_microusd-policy.safety_reserve_microusd-high-held)
    required=0 if instruction else policy.work_estimate_microusd
    if ledger.budget_enforced and reason=="allowed" and (remaining==0 if instruction else remaining<required):
        reason="cumulative_budget_exhausted"
    return WorkPolicyDecision(read_id=read_id,policy_id=policy.policy_id,policy_revision=policy.revision,
        decision="allow" if reason=="allowed" else "deny",reason=reason,observed_at=now,
        budget_id=work_policy_key("cost",policy.authority_domain),
        remaining_microusd=remaining if ledger.budget_enforced else None,
        required_microusd=required,observed_high_water_microusd=high,reserved_microusd=held,
        budget_enforced=ledger.budget_enforced,
        safety_reserve_microusd=policy.safety_reserve_microusd if ledger.budget_enforced else None,
        cumulative_ceiling_microusd=policy.cumulative_ceiling_microusd if ledger.budget_enforced else None)


def microusd_from_decimal(value):
    from decimal import Decimal,InvalidOperation,ROUND_CEILING
    if type(value) is not str:
        raise Problem(422,"WORK_COST_INVALID","reported USD must retain its decimal text, not a binary float")
    try:
        amount=Decimal(value)
        if not amount.is_finite() or amount<0:raise ValueError()
        micro=int((amount*Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation,ValueError,OverflowError):
        raise Problem(422,"WORK_COST_INVALID","finite nonnegative decimal USD required") from None
    if micro>2**63-1:raise Problem(422,"WORK_COST_INVALID","reported cost exceeds its representable bound")
    return micro


class StandingPolicyInstallation(_Model):
    schema_version:Literal["fa-steady-policy-installation-v1"]="fa-steady-policy-installation-v1"
    policy:SteadyWorkPolicy
    operator_oid:Identifier
    decision_id:Identifier
    approved_at:datetime
    change_expires_at:datetime
    initial_observation:CostObservation
    _times=field_validator("approved_at","change_expires_at")(_utc)


class AgentWorkOrigin(_Model):
    schema_version:Literal["fa-agent-work-origin-v1"]="fa-agent-work-origin-v1"
    message_id:Identifier
    sender:TurnStartReceipt
    relay:TurnRelayContext
    refs:Annotated[tuple[PeerTeamReference,...],Field(max_length=8)]=()
    model_call_limit:Annotated[int,Field(ge=1)]
    request:ControlRequest

    @model_validator(mode="after")
    def exact_sender(self):
        if (self.relay.hop<1 or self.relay.chain[-1]!=self.sender.worker.vm.agent_id
                or len(set(self.refs))!=len(self.refs)):
            raise ValueError("an Agent admission retains its original sender and distinct references")
        return self


class WorkRegistration(_Model):
    kind:Literal["steady_work_registration"]="steady_work_registration"
    admission_id:Identifier
    request_id:Identifier
    submit_sha256:Digest
    policy_id:Identifier
    policy_revision:Annotated[int,Field(ge=1)]
    policy_sha256:Digest
    principal_oid:Identifier
    binding:RuntimeBinding
    profile:AssistantProfile
    runtime_policy:PerWorkRuntimePolicy
    hosted_session_id:Identifier|None=Field(default=None,exclude_if=lambda value:value is None)
    profile_selection_id:Identifier|None=Field(default=None,exclude_if=lambda value:value is None)
    environment_transfer_id:Identifier|None=Field(default=None,exclude_if=lambda value:value is None)
    agent_scope:AgentScopeReference|None=Field(default=None,exclude_if=lambda value:value is None)
    turn_authorization:TurnAuthorization|None=Field(default=None,exclude_if=lambda value:value is None)
    routine:RoutineOccurrence|None=Field(default=None,exclude_if=lambda value:value is None)
    agent_vm:AgentVmBinding|None=Field(default=None,exclude_if=lambda value:value is None)
    agent_origin:AgentWorkOrigin|None=Field(default=None,exclude_if=lambda value:value is None)

    @model_validator(mode="after")
    def vm_scope(self):
        vm=self.agent_vm
        if vm is not None and (
                self.profile.instruction is None or vm.team_id!=self.binding.team_id
                or vm.hosted_session_id!=self.hosted_session_id
                or vm.hosted_agent_version!=self.profile.instruction.hosted_agent_version
                or self.agent_scope is not None and vm.agent_id!=self.agent_scope.agent_id):
            raise ValueError("the registered VM must retain its original Team, Agent and Hosted version")
        if self.agent_origin is not None and (
                vm is None or self.profile.instruction is None or self.profile.instruction.peer_work is not None
                or self.routine is not None or self.principal_oid!=self.agent_origin.sender.worker.caller_oid):
            raise ValueError("an Agent work needs its original sender, VM and ordinary instruction")
        return self


def registration_request_digest(record):
    instruction=record.profile.instruction
    peer=None if instruction is None else instruction.peer_work
    return digest([record.request_id,record.submit_sha256,record.policy_sha256,
        *(() if peer is None else (peer.model_dump(mode="json"),)),
        *(() if record.routine is None else (record.routine.model_dump(mode="json"),)),
        *(() if record.agent_vm is None else (record.agent_vm.model_dump(mode="json"),)),
        *(() if record.agent_origin is None else (record.agent_origin.model_dump(mode="json"),))])


def registration_receipt_id(record):
    return (work_policy_key("registration",record.admission_id) if record.agent_origin is None
            else record.agent_origin.request.request_id)


def registered_instruction(record):
    task=record.profile.instruction
    return None if task is None else InstructionSubmit(request_id=record.request_id,content=task.content,
        conversation_id=None if task.turn_number==1 else task.conversation_id,model=task.requested_model,
        attachments=task.attachments,files=task.files)


class WorkRegistry(_Model):
    kind:Literal["steady_work_registry"]="steady_work_registry"
    policy_id:Identifier
    entries:tuple[Identifier,...]=()

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.entries))!=len(self.entries):raise ValueError("duplicate original work in registry")
        return self


class WorkPolicyRevocation(_Model):
    kind:Literal["steady_work_policy_revocation"]="steady_work_policy_revocation"
    policy_id:Identifier
    policy_sha256:Digest
    principal_oid:Identifier
    decision_id:Identifier
    revoked_at:datetime
    _time=field_validator("revoked_at")(_utc)


def configured_prompt_override(policy:SteadyWorkPolicy)->PromptProfile|None:
    import os
    if policy.agent_scope is not None or policy.profile.prompt_profile is not None:
        return None
    value=os.environ.get("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE","fa-instruction-v2")
    if value=="fa-instruction-v1":
        return "fa-instruction-v1"
    if value=="fa-instruction-v2":
        return "fa-instruction-v2"
    raise Problem(503,"INSTRUCTION_DEFAULT_PROMPT_CONFIGURATION","the new-conversation prompt default is unsupported")


def registered_prompt_override(policy:SteadyWorkPolicy,profile:AssistantProfile)->PromptProfile|None:
    if profile.prompt_profile==policy.profile.prompt_profile:
        return None
    if policy.profile.prompt_profile is None and profile.prompt_profile in {"fa-instruction-v1","fa-instruction-v2"}:
        return profile.prompt_profile
    raise Problem(409,"WORK_POLICY_REGISTRY_INVALID","registered prompt is outside the original policy or supported default")


def derive_work_binding(policy,request_id,*,instruction=None,agent_origin=None):
    """Derive identity and work content without selecting or authorizing a profile."""
    if instruction is not None and (type(instruction) is not InstructionSubmit or instruction.request_id!=request_id):
        raise Problem(422,"WORK_POLICY_COMMAND","original typed instruction required")
    seed=work_policy_key("admission",*policy.scope_ids,policy.template.human_oid,request_id)
    named=lambda kind:work_policy_key(kind,seed)
    original=policy.template
    owner=original.owner.model_copy(update={"owner_record_id":named("owner"),"conversation_id":named("conversation"),
                                           "policy_id":named("operation-policy")})
    binding=original.model_copy(update={
        "binding_id":named("binding"),"owner":owner,"event_id":named("event"),"job_id":named("job"),
        "work":original.work.model_copy(update={"work_id":named("work")}),
        "operation_id":named("operation"),"attempt_id":named("attempt"),"grant_id":named("grant"),
        "claim_ids":(policy.profile.claim_id,),
    })
    binding=RuntimeBinding.model_validate_json(binding.model_dump_json())
    if instruction is not None:
        binding=RuntimeBinding.model_validate_json(binding.model_copy(update={"work":binding.work.model_copy(update={
            "goal":("Follow the original human instruction: " if agent_origin is None
                    else "Follow the original agent message: ")+digest(instruction.content),
            "result_kind":"answer","required_checks":TEXT_CHECKS,"human_checks":(),
        })}).model_dump_json())
    return seed,binding


def derive_work(policy,request_id,mode,*,instruction=None,turn_number=1,previous_archive_id=None,model=None,
                hosted_agent_version=None,model_capacity=None,prompt_override:PromptProfile|None=None,profile_template=None,
                memory_capture=None,capability_capture=None,reasoning_summary=None,peer_work=None,role_prompt=None,
                agent_origin=None):
    if agent_origin is not None and (instruction is None or peer_work is not None):
        raise Problem(403,"AGENT_ADMISSION_INVALID","an Agent source requires its ordinary original instruction")
    seed,binding=derive_work_binding(policy,request_id,instruction=instruction,agent_origin=agent_origin)
    named=lambda kind:work_policy_key(kind,seed)
    base_profile=policy.profile
    if peer_work is not None:
        from .behavior_agent_peer_work import narrow_profile
        from .behavior_agent_messages import claim_key
        if (instruction is None or profile_template is not None or memory_capture is not None
                or capability_capture is not None or prompt_override is not None or previous_archive_id is not None
                or turn_number != 1):
            raise Problem(403,"AGENT_MESSAGE_ASSIGNMENT_DENIED","peer work cannot inherit a private conversation or profile")
        base_profile=narrow_profile(base_profile)
        binding=RuntimeBinding.model_validate({**binding.model_dump(),
            "attempt_id":claim_key(peer_work.message_key),"work":{**binding.work.model_dump(),
            "work_id":peer_work.recipient_work_id,"goal":"Follow the original agent handoff: "+peer_work.content_sha256}})
    if profile_template is not None:
        from .behavior_profile_selection import profile_from_template
        if instruction is None or prompt_override is not None:
            raise Problem(422,"PROFILE_SELECTION_UNSUPPORTED","profile selection only applies to an instruction")
        base_profile=profile_from_template(policy,profile_template)
    profile=AssistantProfile.model_validate_json(base_profile.model_copy(update={
        "binding_id":binding.binding_id,"artifact_id":named("artifact"),
        "artifact_version":named("artifact-version")[:32],"max_model_requests":policy.max_model_requests,
    }).model_dump_json())
    if prompt_override is not None:
        if (instruction is None or policy.profile.prompt_profile is not None
                or prompt_override not in {"fa-instruction-v1","fa-instruction-v2"}):
            raise Problem(422,"WORK_POLICY_PROMPT_PROFILE","only the supported default for an unconfigured instruction is allowed")
        profile=AssistantProfile.model_validate_json(
            profile.model_copy(update={"prompt_profile":prompt_override}).model_dump_json())
    if memory_capture is not None:
        from .memory_bridge_settings import apply_memory_capture
        if instruction is None:
            raise Problem(403,"MEMORY_RECALL_SCOPE_DENIED","memory settings apply only to an original instruction")
        profile,binding=apply_memory_capture(profile,binding,seed,memory_capture,selected_template=profile_template)
    if instruction is not None:
        from .behavior_instruction import conversation_id
        if type(instruction) is not InstructionSubmit or instruction.request_id!=request_id:
            raise Problem(422,"WORK_POLICY_COMMAND","original typed instruction required")
        instruction=InstructionSubmit.model_validate_json(instruction.model_dump_json())
        if instruction.attachments and base_profile.text_attachment_profile is None:
            raise Problem(403,"ATTACHMENTS_DISABLED","the original server policy does not enable source attachments")
        if instruction.files and base_profile.user_text_file_profile is None:
            raise Problem(403,"USER_TEXT_FILES_DISABLED","the original server policy does not enable literal text files")
        from .behavior_capabilities import CAPABILITY_BASE_PROFILES,check_captured
        capabilities=check_captured(base_profile.capabilities,capability_capture)
        if capabilities is not None and profile.prompt_profile not in CAPABILITY_BASE_PROFILES:
            raise Problem(422,"CAPABILITY_BASE_UNSUPPORTED","this instruction profile does not compose capabilities")
        from .behavior_capability_prompt import skill_access
        skill_budget=(base_profile.skill_catalog_budget_profile
                      if skill_access(profile.prompt_profile,capabilities) else None)
        from .behavior_agent_role_prompt import role_text_mismatch
        mismatch=role_text_mismatch(profile.prompt_profile,role_prompt)
        if mismatch=="AGENT_ROLE_PROMPT_MISSING":
            raise Problem(409,mismatch,"the agent-role profile needs its saved role prompt")
        if mismatch is not None:
            raise Problem(422,mismatch,"role text applies only to the agent-role profiles")
        task=InstructionTask(content=instruction.content,
                             conversation_id=conversation_id(policy,instruction) if peer_work is None else peer_work.conversation_id,
                             turn_number=turn_number,previous_archive_id=previous_archive_id,
                             model=model or instruction.model,requested_model=instruction.model,
                             hosted_agent_version=hosted_agent_version,
                             checkpoint_profile=base_profile.checkpoint_profile,
                             model_provider=base_profile.model_provider,
                             reasoning_summary=reasoning_summary,
                             prompt_profile=profile.prompt_profile,
                             sdk_start_profile=base_profile.sdk_start_profile,
                             model_capacity=model_capacity,sop_copy=base_profile.sop_copy,
                             skill_catalog_budget_profile=skill_budget,
                             turn_origin_profile=base_profile.turn_origin_profile,
                             worker_profile=base_profile.worker_profile,
                             text_attachment_profile=base_profile.text_attachment_profile,attachments=instruction.attachments,
                             user_text_file_profile=base_profile.user_text_file_profile,files=instruction.files,
                             capabilities=capabilities,peer_work=peer_work,role_prompt=role_prompt)
        profile=profile.model_copy(update={"instruction":task})
    return seed,binding,profile


def derive_registered_work(policy,record,mode,*,profile_template=None):
    """Rebuild captured settings without consulting current process defaults."""
    if (record.agent_scope!=policy.agent_scope or record.policy_id!=policy.policy_id
            or record.policy_revision!=policy.revision or record.policy_sha256!=policy.sha256):
        raise Problem(409,"WORK_POLICY_REGISTRY_INVALID","registered work must retain its original agent and policy version")
    turn=record.profile.instruction
    peer_work=None if turn is None else turn.peer_work
    if (record.profile_selection_id is None)!=(profile_template is None):
        raise Problem(409,"PROFILE_SELECTION_MISSING","the original work needs its exact approved profile selection")
    return derive_work(policy,record.request_id,mode,instruction=registered_instruction(record),
        turn_number=1 if turn is None else turn.turn_number,
        previous_archive_id=None if turn is None else turn.previous_archive_id,
        model=None if turn is None else turn.model,
        hosted_agent_version=None if turn is None else turn.hosted_agent_version,
        model_capacity=None if turn is None else turn.model_capacity,
        reasoning_summary=None if turn is None else turn.reasoning_summary,
        prompt_override=None if turn is None or profile_template is not None or peer_work is not None
        else registered_prompt_override(policy,record.profile),
        profile_template=profile_template,memory_capture=record.profile.memory_scope,
        capability_capture=None if turn is None else turn.capabilities,peer_work=peer_work,
        role_prompt=None if turn is None else turn.role_prompt,agent_origin=record.agent_origin)


def check_dispatch_row(control,admission,binding,profile,work_policy,observation):
    """Refuse an instruction before any write if its later worker dispatch row cannot fit one Table row.

    Builds the WorkDispatchPermission row that WorkerCore.start writes, with the same models and store encoding,
    the real task bytes and read-set shape, and every value that may still grow (generation, owner epoch, cost
    counters, timestamps, evidence ID) at its maximum. The task (message, files, role text, capabilities) never changes.
    """
    import hashlib
    from datetime import timezone
    from .behavior_assistant_wire import Task
    from .behavior_foundry import native_key
    from .behavior_operations import ExecutionContext
    from .behavior_store import _Row,_json
    from .behavior_workers import ExecutorBinding,ExecutorStart
    if type(observation) is not CostObservation:
        return
    raw=Task(job_id=binding.job_id,work_id=binding.work.work_id,definition_digest=digest(binding.work.model_dump(mode="json")),
             profile_digest=digest(profile.model_dump(mode="json")),source=profile.source,artifact_id=profile.artifact_id,
             artifact_version=profile.artifact_version,operation_id=binding.operation_id,
             instruction=profile.instruction).model_dump_json().encode()
    contract=control.runtime.dep.workers.contract
    latest=datetime(9999,12,31,23,59,59,999999,tzinfo=timezone.utc)
    owner=ExecutionContext(**{**{name:getattr(binding.owner,name) for name in ExecutionContext.model_fields},"epoch":2**63-1})
    start_id=worker_key("start",binding.job_id,"8")
    reads=(worker_key("pool",contract.workspace_id,binding.job_id),worker_key("job",binding.job_id),
           control_key("budget",binding.owner.root_budget_id),control_key("owner",binding.owner.owner_record_id),
           control_key("policy",binding.owner.policy_id),control.policy_key,control.revocation_key,control.cost_key,
           work_policy_key("reservation",admission),work_policy_key("registration",admission),
           runtime_key("policy",binding.binding_id),work_policy_key("dispatch",admission),
           native_key("grant",native_key("bootstrap-grant",binding.job_id)))
    start=ExecutorStart(binding=ExecutorBinding(job_id=binding.job_id,work_id=binding.work.work_id,generation=8,owner=owner,
        executor_id=contract.executor_id,executor_version=contract.executor_version,
        input_digest=hashlib.sha256(raw).hexdigest(),start_request_id=start_id),
        request=ControlRequest(request_id=start_id,request_digest="f"*64,read_set=tuple(sorted(set(reads)))),input=raw)
    most=2**63-1
    permission=WorkDispatchPermission(admission_id=admission,start=start,
        policy_after=PerWorkRuntimePolicy.model_validate({**work_policy.model_dump(),"stage":"dispatched",
            "accepted_at":latest,"dispatched_at":latest,"valid_until":None}),
        observation=observation.model_copy(update={"evidence_id":"E"*64,"observed_cumulative_microusd":most,
            "legacy_unresolved_reserve_microusd":most,"observed_at":latest}))
    data=permission.model_dump(mode="json")
    size=len(_json(_Row(binding=control.store._binding,row_id=work_policy_key("dispatch",admission),
                        data=data,material=material(data))))
    limit=control.store._scope.max_payload_bytes
    if size>limit:
        raise Problem(413,"INSTRUCTION_TASK_TOO_LARGE",
                      "the complete turn would not fit its stored dispatch record; no work was accepted",
                      max_bytes=limit,actual_bytes=size)
    return size


# Standing-work and channel refreshes publish into the same runtime binding caches.
_REFRESH_LOCK = threading.Lock()
_REFRESHED = ContextVar("work_policy_refreshed_in_phase", default=None)


@contextmanager
def refresh_once():
    """One read-only phase of one request: each binding's registry refresh runs once, not per authorize.

    Every authorize still reads its owner, policy, revocation and registration rows; nothing outlives the phase.
    """
    if _REFRESHED.get() is not None:
        yield
        return
    token=_REFRESHED.set(set())
    try:
        yield
    finally:
        _REFRESHED.reset(token)


class WorkPolicyControl:
    """One registered standing scope; returned decisions are never write grants."""
    def __init__(self,runtime,policy,cost_reader):
        if (type(policy) is not SteadyWorkPolicy or policy.evidence_mode!=runtime.mode
                or policy.control_binding!=runtime.store._binding or policy.authority_domain!=runtime.store._scope.authority_domain
                or policy.queue_seconds>runtime.dep.delivery.contract.max_queue_seconds or not callable(cost_reader)):
            raise Problem(403,"WORK_POLICY_DISABLED","exact standing policy/store/observation reader required")
        policy=SteadyWorkPolicy.model_validate_json(policy.model_dump_json())
        self.runtime,self.store,self.policy,self.cost_reader=runtime,runtime.store,policy,cost_reader
        self.records={}
        self.profile_selections={}
        self.peer_roots={}
        self.base_bindings=runtime.bindings
        self.base_profiles=runtime.profiles
        self._models=None

    @property
    def models(self):
        if self._models is None:
            from .behavior_instruction_models import InstructionModels
            self._models=InstructionModels.from_environment()
        return self._models

    def _runtime_scope(self):
        if self.policy.agent_scope is None:
            return
        template=self.policy.template
        native=self.runtime.dep.native_launches
        if (self.base_bindings.get(template.binding_id)!=template
                or self.base_profiles.get(template.binding_id)!=self.policy.profile
                or any(binding.agent_oid!=template.agent_oid for binding in self.base_bindings.values())
                or self.runtime.mode=="native" and (
                    native is None or not native.targets or any(target.agent_oid!=template.agent_oid for target in native.targets))):
            raise Problem(503,"AGENT_RUNTIME_SCOPE_NOT_WIRED","the registered peer needs its own runtime; A is not a fallback")

    def initial_records(self,principal,decision_id,change_expires_at):
        self._human(principal,"approve")
        if self.policy.minimum_observed_microusd == 0:
            raise Problem(403,"WORK_POLICY_FRESH_INSTALLER_REQUIRED",
                          "a zero initial floor requires the entirely empty Table installer")
        if self.policy.agent_scope is not None:
            raise Problem(403,"AGENT_SCOPE_REGISTRY_REQUIRED","register the peer against the existing ledger; never initialize another")
        now=self.runtime._now()
        if not now<_utc(change_expires_at)<=now+timedelta(minutes=15):
            raise Problem(403,"WORK_POLICY_CHANGE_EXPIRED","only this initial control transaction has a short permission")
        policy=self.policy
        original_profile=self.base_profiles.get(policy.template.binding_id)
        if (self.base_bindings.get(policy.template.binding_id)!=policy.template or original_profile is None
                or policy.profile.model_copy(update={"claim_id":original_profile.claim_id})!=original_profile):
            raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","original server template differs")
        original=runtime_key("policy",policy.template.binding_id)
        rows=self.store.read_snapshot((original,control_key("budget",policy.template.owner.root_budget_id)))
        if _row(rows,original,RuntimePolicy)!=policy.legacy_template_policy:
            raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","original legacy policy bytes differ")
        root=_row(rows,control_key("budget",policy.template.owner.root_budget_id),BudgetRecord)
        if not root.active or root.root_budget_id!=policy.template.owner.root_budget_id:
            raise Problem(403,"WORK_POLICY_BUDGET_SCOPE","original root budget is required")
        observation=self.cost_reader()
        if type(observation) is not CostObservation:
            raise Problem(403,"WORK_POLICY_COST_UNAVAILABLE","explicit cumulative cost evidence is required")
        ledger=CostLedger(authority_domain=policy.authority_domain,cost_scope_sha256=policy.cost_scope_sha256,
            observed_high_water_microusd=observation.observed_cumulative_microusd,
            legacy_unresolved_reserve_microusd=observation.legacy_unresolved_reserve_microusd,
            legacy_liability_sha256=observation.legacy_liability_sha256,
            latest_observed_at=observation.observed_at,latest_observation_sha256=digest(observation.model_dump(mode="json")))
        decision=cumulative_decision(policy,ledger,observation,now,read_id=decision_id,mode=self.runtime.mode)
        if decision.reason not in {"allowed","cumulative_budget_exhausted"}:
            raise Problem(403,"WORK_POLICY_COST_UNAVAILABLE","fresh exact original cost observation required")
        installation=StandingPolicyInstallation(policy=policy,operator_oid=principal.oid,decision_id=decision_id,
            approved_at=now,change_expires_at=change_expires_at,initial_observation=observation)
        return installation,ledger,WorkRegistry(policy_id=policy.policy_id)

    def install_offline(self,principal,decision_id,change_expires_at):
        if self.runtime.mode!="offline_contract":
            raise Problem(403,"WORK_POLICY_NATIVE_INSTALLER_REQUIRED","native requires its exact historical cutover")
        values=self.initial_records(principal,decision_id,change_expires_at)
        keys=(self.policy_key,self.cost_key,self.registry_key)
        request=ControlRequest(request_id=work_policy_key("install",self.policy.authority_domain),
            request_digest=digest([v.model_dump(mode="json") for v in values]),read_set=keys)
        def decide(rows):
            if any(rows[k] is not None for k in keys):
                raise Problem(409,"WORK_POLICY_ALREADY_INSTALLED","standing scope and cumulative cost cannot reset")
            if self.runtime._now()>=change_expires_at:
                raise Problem(403,"WORK_POLICY_CHANGE_EXPIRED","initial control transaction expired")
            return ControlChanges(writes=tuple(_write(k,v,"create",material(v.model_dump(mode="json")) if k==self.policy_key else None)
                for k,v in zip(keys,values)),result={"policy_id":self.policy.policy_id,"installed":True,"business_deadline":None})
        result=self.store.transact(request,decide)
        self.runtime.work_policy=self
        self.runtime.historical_binding_ids=frozenset((*self.runtime.historical_binding_ids,*self.base_bindings))
        self.runtime.dep.inbox.register_work_policy(self)
        self.runtime.dep.delivery.work_policy=self
        self.runtime.dep.workers.work_policy=self
        return result

    def attach(self):
        self._runtime_scope()
        self.installed()
        self.runtime.work_policy=self
        self.runtime.historical_binding_ids=frozenset((*self.runtime.historical_binding_ids,*self.base_bindings))
        self.runtime.dep.inbox.register_work_policy(self)
        self.runtime.dep.delivery.work_policy=self
        self.runtime.dep.workers.work_policy=self
        self.refresh()

    def result_route(self,request):
        self.refresh()
        binding=request.path_params.get("binding_id")
        if binding not in self.records:return False
        path=getattr(request.scope.get("route"),"path",None)
        root="/behavior/v1/bindings/{binding_id}"
        return (request.method=="GET" and path==root+"/jobs/{job_id}"
                and request.path_params.get("job_id")==self.records[binding].binding.job_id or
                request.method=="POST" and path in {root+"/delivery/pull",root+"/delivery/acknowledge",
                    root+"/delivery/pull-continue",root+"/delivery/pull-reconcile"})


    def submit(self,principal,command,*,routine=None,agent_vm=None):
        import hashlib
        from .behavior_assistant_wire import AssistantSubmit
        if type(command) not in {AssistantSubmit,InstructionSubmit}:
            raise Problem(422,"WORK_POLICY_COMMAND","original typed human submission required")
        checked=type(command).model_validate_json(command.model_dump_json())
        record=self.register(principal,checked.request_id,hashlib.sha256(checked.model_dump_json().encode()).hexdigest(),
                             instruction=checked if type(checked) is InstructionSubmit else None,                             routine=routine,agent_vm=agent_vm)
        jobkey=worker_key("job",record.binding.job_id)
        if self.store.read_snapshot((jobkey,))[jobkey] is not None:
            return record.binding,self.runtime.job_status(record.binding.binding_id,principal)
        return record.binding,self.runtime.submit(record.binding.binding_id,checked,principal)

    def cancel(self,principal,binding_id):
        self._human(principal)
        self.refresh()
        if binding_id not in self.records:
            raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","only this standing policy's original work may be cancelled")
        binding,_,context=self.runtime.authorize(binding_id,principal,"read")
        job=self.runtime.dep.workers.inspect(binding.job_id)
        receipt=self.runtime.dep.workers.cancel(binding.job_id,context,expected_binding=job.binding)
        if self.runtime.dep.workers.inspect(binding.job_id).state=="cancelled":
            self.runtime.controller.step(binding_id)
        return receipt

    def cancellation_receipt(self, principal, binding_id, generation):
        self._human(principal)
        self.refresh()
        if binding_id not in self.records:
            raise Problem(403, "WORK_POLICY_SCOPE_CHANGED", "only this standing policy's original work may be read")
        binding, _, context = self.runtime.authorize(binding_id, principal, "read")
        return self.runtime.dep.workers.read_cancellation_receipt(binding.job_id, context, generation)

    def submit_reconcile(self,principal,command):
        import hashlib
        self._human(principal)
        self.refresh()
        _,binding=derive_work_binding(self.policy,command.request_id,
            instruction=command if type(command) is InstructionSubmit else None)
        saved=self.records.get(binding.binding_id)
        command_sha=hashlib.sha256(command.model_dump_json().encode()).hexdigest()
        if saved is not None and saved.submit_sha256!=command_sha:
            raise Problem(409,"WORK_POLICY_REQUEST_CONFLICT","original submission bytes differ")
        if saved is None:
            policy=self.installed()
            if not policy.active or not policy.read_enabled:
                raise Problem(403,"WORK_POLICY_REVOKED","current original rights are revoked")
            state="not_admitted"
        else:
            self.runtime.authorize(saved.binding.binding_id,principal,"read")
            key=worker_key("job",binding.job_id)
            if self.store.read_snapshot((key,))[key] is not None:
                return saved.binding,self.runtime.job_status(saved.binding.binding_id,principal)
            plan=self.runtime._existing(saved.binding,"admission",principal,command)
            receipt=None if plan is None else self.store._receipt(plan.request)
            event=control_key("delivery-event",binding.event_id)
            obligation=control_key("delivery-obligation",binding.obligation_id)
            effects=self.store.read_snapshot((event,obligation))
            if receipt is None and any(value is not None for value in effects.values()):
                raise Problem(503,"WORK_SUBMIT_UNKNOWN","admission material lacks its original receipt; do not resend")
            state="not_admitted" if receipt is None else "admission_incomplete"
        return WorkSubmitObservation(schema_version="fa-work-submit-observation-v1",
            request_id=command.request_id,submit_sha256=command_sha,state=state,retry_same_request=True)

    def submission_result(self,binding,status):
        turn=self.records[binding.binding_id].profile.instruction
        return {"binding_id":binding.binding_id,"status":status,**({} if turn is None else {
            "conversation_id":turn.conversation_id,"turn_number":turn.turn_number,"model":turn.model})}

    def _instruction_record(self,principal,identifier,turn_number):
        self._human(principal)
        def selected():
            return next((r for r in self.records.values() if r.profile.instruction is not None
                and r.profile.instruction.conversation_id==identifier
                and r.profile.instruction.turn_number==turn_number),None)
        cached=selected()
        # The cached ID only narrows lookup; current rows and authorization are still rechecked.
        self.refresh(None if cached is None else cached.binding.binding_id)
        record=selected()
        if record is None:raise Problem(404,"CONVERSATION_NOT_FOUND","original conversation turn is unavailable")
        self.runtime.authorize(record.binding.binding_id,principal,"read")
        return record

    def read_delegation(self,principal,identifier,turn_number,authority):
        """Read the original child inside the caller's current-read snapshot."""
        from .behavior_assistant import key as assistant_key,_Session
        from .behavior_delegation import DelegationIntent,delegation_key,state_key,result_key
        from .behavior_delegation_api import _scope,_reply
        record=self._instruction_record(principal,identifier,turn_number)
        key=delegation_key(record.binding.work.work_id)
        rows=self.store.read_snapshot((key,state_key(key),result_key(key)))
        if rows[key] is None:
            if rows[state_key(key)] is not None or rows[result_key(key)] is not None:
                raise Problem(503,"DELEGATION_UNKNOWN","the original delegation intent is unavailable")
            return None
        intent=_row(rows,key,DelegationIntent)
        if (intent.parent.binding_id!=record.binding.binding_id
                or intent.parent.parent_request_id!=record.request_id
                or intent.parent.execution.job_id!=record.binding.job_id):
            raise Problem(409,"DELEGATION_SCOPE_CHANGED","the original parent turn must match")
        if authority is None or authority.runtime is not self.runtime:
            raise Problem(503,"DELEGATION_UNAVAILABLE","the original assistant reader is unavailable")
        skey=assistant_key("session",intent.parent.session_id)
        sessions=self.store.read_snapshot((skey,))
        if sessions[skey] is None:
            raise Problem(503,"DELEGATION_UNKNOWN","the original parent session is unavailable")
        session=_row(sessions,skey,_Session)
        if session.assignment.task.instruction!=record.profile.instruction:
            raise Problem(409,"DELEGATION_SCOPE_CHANGED","the original instruction must match")
        ledger,parent=_scope(authority,session)
        # The route owns the consistent read, including current human authorization.
        return _reply(ledger._read(parent))

    def read_reply(self,principal,identifier,turn_number):
        from .behavior_assistant import key as assistant_key
        from .behavior_delivery import VerifiedWorkResult
        from .behavior_instruction import SavedReply,unpack_reply,reply_reference,sdk_session_id
        record=self._instruction_record(principal,identifier,turn_number)
        binding=record.binding
        key=assistant_key("result",binding.work.work_id)
        row=self.store.read_snapshot((key,))[key]
        if row is None:raise Problem(404,"INSTRUCTION_REPLY_NOT_READY","no complete original SDK reply is saved")
        result=_parse(row.payload["data"],VerifiedWorkResult)
        if row.payload["material"]!=material(result.model_dump(mode="json")).model_dump(mode="json"):
            raise Problem(409,"INSTRUCTION_REPLY_INVALID","original saved SDK reply differs")
        reply_key=assistant_key("reply",binding.job_id)
        saved_row=self.store.read_snapshot((reply_key,))[reply_key]
        if saved_row is None:raise Problem(404,"INSTRUCTION_REPLY_NOT_READY","no complete SDK reply was produced")
        saved=_parse(saved_row.payload["data"],SavedReply)
        reply=unpack_reply(saved)
        if (saved.job_id!=binding.job_id or result.work_id!=binding.work.work_id
                or result.definition_digest!=digest(binding.work.model_dump(mode="json"))
                or result.content!=reply_reference(saved) or reply.sdk_session_id!=sdk_session_id(identifier)
                or reply.conversation_id!=identifier or reply.turn_number!=turn_number):
            raise Problem(409,"INSTRUCTION_REPLY_INVALID","original SDK reply belongs to another turn")
        return reply

    def _refresh_jobs(self,jobs):
        # A full refresh reads every turn this agent ever registered and outgrows the pure-read byte bound;
        # cached turns are re-read one by one. Uncached work (admitted elsewhere) still needs the full read.
        cached={r.binding.job_id:r.binding.binding_id for r in self.records.values()}
        if not jobs or any(job not in cached for job in jobs):
            self.refresh()
            return
        for job in jobs:
            self.refresh(cached[job])

    def conversation(self,principal,identifier):
        from .behavior_instruction import Conversation,conversation_key
        from .behavior_assistant import key as assistant_key,_Session
        from .behavior_delivery import _Obligation
        self._human(principal)
        ckey=conversation_key(identifier)
        row=self.store.read_snapshot((ckey,))[ckey]
        if row is None:raise Problem(404,"CONVERSATION_NOT_FOUND","original conversation is unavailable")
        conversation=_parse(row.payload["data"],Conversation)
        self._refresh_jobs(conversation.jobs)
        b=self.policy.template
        if (conversation.human_oid,conversation.team_id,conversation.agent_oid)!=(b.human_oid,b.team_id,b.agent_oid):
            raise Problem(403,"CONVERSATION_SCOPE_DENIED","original conversation is outside this human's scope")
        records={r.binding.job_id:r for r in self.records.values()}
        turns=[];job=None
        for number,job_id in enumerate(conversation.jobs,1):
            r=records[job_id];binding=r.binding
            self.runtime.authorize(binding.binding_id,principal,"read")
            jkey=worker_key("job",job_id)
            jobrow=self.store.read_snapshot((jkey,))[jkey]
            job=None if jobrow is None else _parse(jobrow.payload["data"],JobRecord)
            sid=None if job is None or job.binding is None else assistant_key("session-id",job.binding.start_request_id)
            skey=assistant_key("session",sid) if sid else None
            orow=self.store.read_snapshot((control_key("delivery-obligation",binding.obligation_id),))[control_key("delivery-obligation",binding.obligation_id)]
            sessionrow=None if skey is None else self.store.read_snapshot((skey,))[skey]
            session=None if sessionrow is None else _parse(sessionrow.payload["data"],_Session)
            obligation=None if orow is None else _parse(orow.payload["data"],_Obligation)
            reply_key=assistant_key("reply",job_id)
            replyrow=self.store.read_snapshot((reply_key,))[reply_key]
            from .behavior_assistant_wire import PublicProgress
            progress=PublicProgress()
            if sid is not None:
                from .behavior_instruction_progress import ProgressRecord
                pkey=assistant_key("progress",sid)
                prow=self.store.read_snapshot((pkey,))[pkey]
                if prow is not None:
                    saved=_parse(prow.payload["data"],ProgressRecord)
                    if saved.job_id!=job_id or saved.conversation_id!=identifier or saved.turn_number!=number:
                        raise Problem(409,"CONVERSATION_CONTEXT_CHANGED","public progress belongs to another turn")
                    progress=saved.progress
            turns.append({"request_id":r.request_id,"binding_id":binding.binding_id,"job_id":job_id,
                "work_id":binding.work.work_id,"turn_number":number,"state":"admitting" if job is None else job.state,
                "model_requests":0 if session is None else session.model_requests,
                "reply_available":job is not None and job.result_forwarded and replyrow is not None,"model":r.profile.instruction.model,
                "delivery_id":None if obligation is None else obligation.final_delivery_id,
                "progress":progress.model_dump(mode="json")})
            if r.profile.instruction.model_capacity is not None:
                turns[-1]["model_capacity"] = r.profile.instruction.model_capacity.model_dump(mode="json")
            if r.profile.instruction.prompt_profile is not None:
                from .behavior_instruction import TurnArchive,turn_material
                from .behavior_skill_prompt import assemble_prompt
                archive_key=assistant_key("text-archive",job_id)
                archive_row=self.store.read_snapshot((archive_key,))[archive_key]
                if archive_row is not None:
                    saved=_parse(archive_row.payload["data"],TurnArchive)
                    expected=assemble_prompt(r.profile.instruction.prompt_profile).record
                    if (saved.job_id!=job_id or saved.conversation_id!=identifier or saved.turn_number!=number
                            or saved.prompt_assembly!=expected
                            or archive_row.payload["material"]!=turn_material(saved).model_dump(mode="json")):
                        raise Problem(409,"CONVERSATION_CONTEXT_CHANGED","saved instruction assembly differs")
                    turns[-1]["prompt_assembly"]=saved.prompt_assembly.model_dump(mode="json")
                    if saved.sop_copy_outcome is not None:
                        turns[-1]["sop_copy_outcome"] = saved.sop_copy_outcome.model_dump(mode="json")
                    if saved.skill_draft_outcome is not None:
                        turns[-1]["skill_draft_outcome"] = saved.skill_draft_outcome.model_dump(mode="json")
        completed=bool(turns) and job is not None and job.state in {"succeeded","failed","cancelled"} and job.result_forwarded
        ready=(completed
               and conversation.active_job_id is None and conversation.archive_id is not None)
        blocker=None if ready else "turn_unresolved"
        if completed and conversation.active_job_id is None and conversation.archive_id is None:
            from .behavior_instruction_no_sdk import NoSdkTurn
            nkey = assistant_key("no-sdk-turn", conversation.jobs[-1])
            no_sdk = self.store.read_snapshot((nkey,))[nkey]
            if no_sdk is not None:
                saved = _parse(no_sdk.payload["data"], NoSdkTurn)
                if (saved.conversation_id != identifier or saved.job_id != conversation.jobs[-1]
                        or saved.turn_number != len(conversation.jobs)
                        or no_sdk.payload["material"] != material(saved.model_dump(mode="json")).model_dump(mode="json")):
                    raise Problem(409, "CONVERSATION_CONTEXT_CHANGED", "original no-SDK failure material differs")
                blocker = "SDK_NOT_STARTED_RESET_REQUIRED"
        if conversation.reset_request_id is not None:
            ready=False;blocker="CONVERSATION_ARCHIVED"
        elif conversation.hosted_session_id is None:
            ready=False;blocker="CONVERSATION_SESSION_RESET_REQUIRED"
        if ready:
            from .behavior_instruction import TurnArchive
            from .behavior_assistant_checkpoint import unpack
            archive_key=conversation.archive_id
            archived=_row(self.store.read_snapshot((archive_key,)),archive_key,TurnArchive)
            from .behavior_conversation_transfer import current_transfer, transferred_failure, _archive_matches
            transfer = current_transfer(self, conversation)
            restored_failure = transferred_failure(self, conversation, job)
            if (archived.conversation_id!=identifier or archived.job_id!=conversation.jobs[-1]
                    and not (restored_failure and _archive_matches(transfer, archived))):
                raise Problem(409,"CONVERSATION_CONTEXT_CHANGED","saved SDK context differs")
            from .behavior_assistant_wire import SdkArchive,StoredSdkArchive
            if not isinstance(archived.archive,(SdkArchive,StoredSdkArchive)):
                ready=False
                blocker=archived.archive.error
            else:
                if archived.archive.sdk_session_id!=conversation.sdk_session_id:
                    raise Problem(409,"CONVERSATION_CONTEXT_CHANGED","saved SDK session differs")
                if isinstance(archived.archive,SdkArchive):
                    unpack(archived.archive)
                else:
                    from .behavior_sdk_checkpoint import checkpoint_key,SdkStateUpload
                    stored=self.store.read_snapshot((checkpoint_key(archived.job_id),))
                    if _row(stored,checkpoint_key(archived.job_id),SdkStateUpload).reference!=archived.archive:
                        raise Problem(409,"SDK_STATE_INCOMPLETE","stored checkpoint pointer differs")
                    try:
                        from .behavior_profile_selection import choice_for_conversation, profile_from_template
                        choice = choice_for_conversation(self, conversation)
                        consumer = self.policy.profile if choice is None else profile_from_template(self.policy, choice.command.template)
                        archived.archive.require_profile(consumer.checkpoint_profile, model_provider=consumer.model_provider)
                    except Problem as error:
                        if error.body["error"]["code"] != "SDK_STATE_UNSUPPORTED_FORMAT":
                            raise
                        ready=False
                        blocker="SDK_STATE_UNSUPPORTED_FORMAT"
            if archived.sop_copy_outcome is not None and archived.sop_copy_outcome.unresolved_operations:
                ready=False
                blocker="SOP_COPY_OUTCOME_UNKNOWN"
            if archived.skill_draft_outcome is not None and archived.skill_draft_outcome.unresolved_operations:
                ready=False
                blocker="SKILL_DRAFT_OUTCOME_UNKNOWN"
        if conversation.reset_request_id is None:
            from .behavior_foundry import session_version_failure, lost_session_failure
            failure = session_version_failure(self.store, job)
            from .behavior_conversation_transfer import transferred_failure
            if failure is not None and not transferred_failure(self, conversation, job):
                ready=False
                blocker=failure if job.result_forwarded else "CONVERSATION_FAILURE_DELIVERY_PENDING"
            elif failure is None and lost_session_failure(self.store, job) is not None:
                # The Hosted compute stopped mid-turn: its outcome is unknown and nothing is resent.
                ready=False
                blocker="ASSISTANT_SESSION_LOST" if job.result_forwarded else "CONVERSATION_FAILURE_DELIVERY_PENDING"
        return {"conversation_id":identifier,"sdk_session_id":conversation.sdk_session_id,
                "can_follow_up":ready,"follow_up_blocker":blocker,"turns":turns,
                "hosted_session_id":conversation.hosted_session_id,
                "hosted_agent_version":conversation.hosted_agent_version,
                "archived":conversation.reset_request_id is not None,
                "can_reset":completed and conversation.reset_request_id is None}

    @property
    def policy_key(self):
        return work_policy_key("policy",*self._key_scope())

    @property
    def cost_key(self):
        return work_policy_key("cost",self.policy.authority_domain)

    @property
    def registry_key(self):
        return work_policy_key("registry",*self._key_scope())

    @property
    def revocation_key(self):
        return work_policy_key("revocation",*self._key_scope())

    def _key_scope(self):
        if isinstance(self.policy,SteadyWorkPolicy):
            return self.policy.scope_ids
        # Cold readers carry only the authority domain and the original registrations.
        scopes=tuple(record.agent_scope for record in self.records.values())
        if not scopes or any(scope!=scopes[0] for scope in scopes):
            raise Problem(409,"WORK_POLICY_REGISTRY_INVALID","history keys require one captured agent scope")
        return (self.policy.authority_domain,*(() if scopes[0] is None else (scopes[0].agent_id,)))

    def _human(self,principal,action="read"):
        b=self.policy.template
        if (not isinstance(principal,BehaviorPrincipal) or principal.kind!="human" or principal.actor_id is not None
                or principal.tenant_id!=b.tenant_id or principal.oid!=b.human_oid):
            raise Problem(403,"WORK_POLICY_HUMAN_REQUIRED","the original designated human is required")
        principal.require(b.team_id,action)

    def installed(self,rows=None):
        rows=self.store.read_snapshot((self.policy_key,self.revocation_key)) if rows is None else rows
        if self.revocation_key not in rows:
            raise Problem(403,"WORK_POLICY_CURRENT_STATE_MISSING","current revocation predicate is required")
        if self.policy.agent_scope is None:
            installed=_row(rows,self.policy_key,StandingPolicyInstallation)
        else:
            from .behavior_agent_registry import AgentScopeRegistry
            installed=_row(rows,self.policy_key,AgentScopeRegistry)
            if installed.policy!=self.policy:
                raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","registered peer policy/version differs")
        if (installed.policy.policy_id!=self.policy.policy_id or installed.policy.template!=self.policy.template
                or installed.policy.profile!=self.policy.profile or installed.operator_oid!=self.policy.template.human_oid):
            raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","installed original standing scope differs")
        if rows[self.revocation_key] is not None:
            revoked=_row(rows,self.revocation_key,WorkPolicyRevocation)
            if revoked.policy_sha256!=installed.policy.sha256 or revoked.principal_oid!=installed.policy.template.human_oid:
                raise Problem(409,"WORK_POLICY_REVOCATION_INVALID","original policy revocation differs")
            return installed.policy.model_copy(update={"active":False,"read_enabled":False})
        return installed.policy

    def accounting(self,principal):
        """Project only retained accounting fields; never refresh cost or authorize work."""
        self._human(principal)
        def read():
            rows=self.store.read_snapshot((self.policy_key,self.revocation_key,self.cost_key))
            policy=self.installed(rows)
            if not policy.active or not policy.read_enabled:
                raise Problem(403,"WORK_POLICY_REVOKED","current standing work/read authority is required")
            if policy.sha256!=self.policy.sha256:
                raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","the configured standing policy differs")
            ledger=_row(rows,self.cost_key,CostLedger)
            if (ledger.authority_domain!=policy.authority_domain
                    or ledger.cost_scope_sha256!=policy.cost_scope_sha256):
                raise Problem(409,"WORK_POLICY_BUDGET_SCOPE","the original cumulative cost authority differs")
            now=self.runtime._now()
            if ledger.latest_observed_at>now:
                raise Problem(409,"WORK_ACCOUNTING_OBSERVATION_INVALID","retained observation is later than this read")
            record=rows[self.cost_key]
            raw=record.payload["data"]
            amounts=("observed_high_water_microusd","legacy_unresolved_reserve_microusd",
                     "work_reservations_microusd","model_pending_microusd","model_metered_microusd")
            fields=(*amounts,"unit","reservation_count","budget_enforced",
                    "latest_observed_at","latest_observation_sha256")
            presence={name:name in raw for name in fields}
            guard,guard_etag=self.store._guard()
            return {
                "schema_version":"fa-work-accounting-read-v1","read_at":now,"unit":raw.get("unit"),
                # CostLedger's historical defaults must not invent values absent from the row.
                "components":{name:raw.get(name) for name in amounts},
                "reservation_count":raw.get("reservation_count"),"budget_enforced":raw.get("budget_enforced"),
                "field_presence":presence,"all_fields_present":all(presence.values()),
                "source":{
                    "latest_observed_at":ledger.latest_observed_at,
                    "latest_observation_sha256":ledger.latest_observation_sha256,
                    "age_seconds":(now-ledger.latest_observed_at).total_seconds(),
                },
                "version":{
                    "ledger_etag":record.etag,"ledger_data_sha256":digest(raw),
                    "policy_etag":rows[self.policy_key].etag,"policy_sha256":policy.sha256,
                    "policy_revision":policy.revision,"guard_revision":guard.revision,"guard_etag":guard_etag,
                },
                "authorization":False,"provider_hard_cap":False,"headroom_resolved":False,
            }
        from .behavior_assistant import _read_conflicts
        return _read_conflicts(lambda:self.store.consistent_read(read),phase="accounting_query")

    def decide(self,principal,request_id,*,read_id):
        self._human(principal)
        rows=self.store.read_snapshot((self.policy_key,self.revocation_key,self.cost_key))
        policy=self.installed(rows)
        return cumulative_decision(policy,_row(rows,self.cost_key,CostLedger),self.cost_reader(),self.runtime._now(),
                                   read_id=read_id,mode=self.runtime.mode)

    def source_ready(self,principal,policy):
        context=self.runtime._reviewed_context(policy.template,policy.legacy_template_policy,principal,(policy.profile.claim_id,))
        expected=self.runtime._reference(policy.profile.source,policy.template,policy.legacy_template_policy,principal)
        if expected not in context.manifest.sources or not all(claim.human_reviewed for claim in context.claims):
            raise Problem(403,"WORK_POLICY_SOURCE_UNAVAILABLE","the exact standing source needs its original human review")
        return context

    def conversation_memory(self,principal,conversation):
        from .memory_bridge_settings import memory_for_conversation
        return memory_for_conversation(self,principal,conversation)

    def preflight(self,principal,query):
        import hashlib
        from .behavior_assistant_wire import AssistantSubmit
        from .behavior_workers import _Pool
        self._human(principal)
        if type(query) is not WorkPreflightQuery:
            raise Problem(422,"WORK_POLICY_QUERY","typed exact preflight query required")
        submitted=(AssistantSubmit(request_id=query.request_id) if query.content is None else InstructionSubmit(
            request_id=query.request_id,content=query.content,conversation_id=query.conversation_id,model=query.model,
            attachments=query.attachments,files=query.files))
        if query.content is None and (query.attachments or query.files):
            raise Problem(422,"ATTACHMENT_FORMAT","attachments require an original instruction")
        expected=hashlib.sha256(submitted.model_dump_json().encode()).hexdigest()
        if query.submit_body_sha256!=expected:
            raise Problem(409,"WORK_POLICY_REQUEST_CONFLICT","canonical fixed submission bytes differ")
        self.refresh()
        instruction=submitted if type(submitted) is InstructionSubmit else None
        admission,binding=derive_work_binding(self.policy,query.request_id,instruction=instruction)
        keys=(self.policy_key,self.revocation_key,self.cost_key,control_key("budget",binding.owner.root_budget_id),
              worker_key("pool",self.runtime.dep.workers.contract.workspace_id),worker_key("job",binding.job_id),
              work_policy_key("registration",admission),runtime_key("policy",binding.binding_id))
        guard=self.store._guard()
        rows=self.store.read_snapshot(keys)
        policy=self.installed(rows);ledger=_row(rows,self.cost_key,CostLedger)
        root=_row(rows,control_key("budget",binding.owner.root_budget_id),BudgetRecord)
        pool=_row(rows,worker_key("pool",self.runtime.dep.workers.contract.workspace_id),_Pool)
        existing=rows[worker_key("job",binding.job_id)] is not None
        if existing:
            self.runtime.authorize(binding.binding_id,principal,"read")
            remaining=max(0,policy.cumulative_ceiling_microusd-policy.safety_reserve_microusd-
                          ledger.observed_high_water_microusd-ledger.legacy_unresolved_reserve_microusd-ledger.work_reservations_microusd)
            money=WorkPolicyDecision(read_id=query.read_id,policy_id=policy.policy_id,policy_revision=policy.revision,
                decision="allow",reason="original_admission_replay_only",observed_at=self.runtime._now(),
                budget_id=self.cost_key,remaining_microusd=remaining if ledger.budget_enforced else None,required_microusd=0,
                observed_high_water_microusd=ledger.observed_high_water_microusd,
                reserved_microusd=ledger.legacy_unresolved_reserve_microusd+ledger.work_reservations_microusd,
                budget_enforced=ledger.budget_enforced,
                safety_reserve_microusd=policy.safety_reserve_microusd if ledger.budget_enforced else None,
                cumulative_ceiling_microusd=policy.cumulative_ceiling_microusd if ledger.budget_enforced else None)
        else:
            money=cumulative_decision(policy,ledger,self.cost_reader(),self.runtime._now(),read_id=query.read_id,
                                      mode=self.runtime.mode,instruction=instruction is not None)
        allowed,reason=money.decision,money.reason
        if not existing and allowed=="allow" and instruction is None:
            try:self.source_ready(principal,policy)
            except Problem as error:
                allowed,reason="deny",error.body["error"]["code"]
        if not existing and instruction is None and (not root.active or root.remaining_attempts<policy.max_model_requests+3
            or root.remaining_units<policy.max_model_requests+2+self.runtime.dep.workers.contract.reservation_units):
            allowed,reason="deny","abstract_root_budget_exhausted"
        turn=None
        memory_ref=None
        if instruction is not None:
            if rows[work_policy_key("registration",admission)] is not None:
                saved=self.records[binding.binding_id]
                if saved.submit_sha256!=expected:
                    raise Problem(409,"WORK_POLICY_REQUEST_CONFLICT","original input differs")
                if not existing:
                    self.runtime.authorize(binding.binding_id,principal,"read")
                binding=saved.binding
                turn=saved.profile.instruction
                memory_ref=memory_setting_reference(admission,saved.profile.memory_scope,captured=True)
            else:
                from .behavior_instruction import instruction_state
                try:
                    conversation,_=instruction_state(self,instruction)
                    from .behavior_profile_selection import choice_for_conversation
                    choice=choice_for_conversation(self,conversation)
                    memory=self.conversation_memory(principal,conversation)
                    from .behavior_agent_role_prompt_versions import captured_role_prompt
                    from .behavior_profile_selection import profile_from_template
                    selected=self.policy.profile if choice is None else profile_from_template(self.policy,choice.command.template)
                    role_prompt=captured_role_prompt(self.store,self.policy,selected.prompt_profile)
                    _,binding,profile=derive_work(self.policy,query.request_id,self.runtime.mode,instruction=instruction,
                        turn_number=len(conversation.jobs)+1,profile_template=None if choice is None else choice.command.template,
                        memory_capture=memory.value,role_prompt=role_prompt)
                    turn=profile.instruction
                    memory_ref=memory_setting_reference(admission,memory.value,captured=False)
                except Problem as error:
                    # A role Agent without a saved role is a clear 409, not a deny the workbench cannot place.
                    if error.body["error"]["code"] in {"AGENT_ROLE_PROMPT_MISSING","AGENT_ROLE_PROMPT_INVALID"}:
                        raise
                    allowed,reason="deny",error.body["error"]["code"]
        scoped=(_row(rows,runtime_key("policy",binding.binding_id),PerWorkRuntimePolicy)
                if rows[runtime_key("policy",binding.binding_id)] is not None else None)
        if self.store._guard()!=guard:
            raise Problem(412,"WORK_POLICY_READ_CONFLICT","pure preflight snapshot changed; it is not permission")
        return WorkPreflight(read_id=query.read_id,request_id=query.request_id,submit_body_sha256=expected,
            configuration_sha256=policy.configuration_sha256,policy_sha256=policy.sha256,
            tenant_id=binding.tenant_id,team_id=binding.team_id,human_oid=binding.human_oid,
            template_binding_id=policy.template.binding_id,binding=binding,decision=allowed,reason=reason,
            observed_at=self.runtime._now(),guard_revision=guard[0].revision,already_admitted=existing,
            capacity_available=any(slot.binding is None for slot in pool.slots),monetary_budget=money,
            execution_deadline=None if scoped is None or scoped.stage!="dispatched" else scoped.valid_until,
            queue_deadline=None if scoped is None else scoped.queue_deadline,
            abstract_attempts_remaining=root.remaining_attempts,abstract_units_remaining=root.remaining_units,
            conversation_id=None if turn is None else turn.conversation_id,
            turn_number=None if turn is None else turn.turn_number,
            model=None if turn is None else (turn.model or self.models.select(instruction.model).id),
            memory_setting_ref=memory_ref)

    def register(self,principal,request_id,submit_sha256,*,instruction=None,peer_work=None,routine=None,agent_vm=None):
        from .behavior_work_registry import head_key
        self._runtime_scope()
        peer_root=None
        if peer_work is None:
            self._human(principal,"submit")
        else:
            from .behavior_agent_peer_work import authorize_registration
            peer_root=authorize_registration(self,principal,peer_work,request_id,instruction)
        registry_head=head_key(self.registry_key)
        rows=self.store.read_snapshot((self.policy_key,self.revocation_key,self.cost_key,self.registry_key,registry_head))
        admission_registry={name:rows[name] for name in (self.registry_key,registry_head)}
        policy=self.installed(rows)
        if routine is not None:
            from .behavior_routine import validate_submission
            if type(routine) is not RoutineOccurrence or peer_work is not None or type(instruction) is not InstructionSubmit:
                raise Problem(403,"ROUTINE_SOURCE_CHANGED","a routine requires its original owner instruction")
        admission,binding,profile=derive_work(policy,request_id,self.runtime.mode)
        key=work_policy_key("registration",admission)
        prior=self.store.read_snapshot((key,))[key]
        if prior is not None:
            if not policy.active or not policy.read_enabled:
                raise Problem(403,"WORK_POLICY_REVOKED","current original rights are revoked")
            saved=_parse(prior.payload["data"],WorkRegistration)
            if saved.request_id!=request_id or saved.submit_sha256!=submit_sha256 or saved.principal_oid!=principal.oid:
                raise Problem(409,"WORK_POLICY_REQUEST_CONFLICT","same work ID has different original input")
            if saved.routine!=routine:
                raise Problem(409,"WORK_POLICY_REQUEST_CONFLICT","the original work has a different occurrence source")
            if agent_vm is not None and saved.agent_vm!=agent_vm:
                raise Problem(409,"WORK_POLICY_REQUEST_CONFLICT","the original request cannot be rebound to another VM")
            if peer_work is not None and (saved.profile.instruction is None or saved.profile.instruction.peer_work!=peer_work):
                raise Problem(409,"AGENT_MESSAGE_ASSIGNMENT_DENIED","the original receiver work has a different peer source")
            self.records[saved.binding.binding_id]=saved
            self.refresh(saved.binding.binding_id)
            return saved
        if routine is not None:
            validate_submission(policy,principal.oid,instruction,routine,now=self.runtime._now())
        conversation=None
        conversation_row=None
        conversation_reads=()
        environment_transfer=None
        profile_choice=None
        profile_reads=()
        profile_snapshot={}
        memory_capture=None
        memory_snapshot={}
        peer_snapshot={}
        captured_vm=None
        if instruction is not None and peer_work is None:
            if type(instruction) is not InstructionSubmit or instruction.request_id!=request_id:
                raise Problem(422,"WORK_POLICY_COMMAND","original typed instruction required")
            if instruction.images:
                # Attached images reach the model only on native turns; never drop them silently here.
                raise Problem(422,"WORK_POLICY_COMMAND","attached images need a native Agent turn")
            from .behavior_instruction import instruction_state,conversation_key
            conversation,conversation_row=instruction_state(self,instruction,registry_rows=rows)
            captured_vm=conversation.agent_vm
            if agent_vm is not None:
                agent_vm=AgentVmBinding.model_validate_json(agent_vm.model_dump_json())
                from .behavior_vm_assignment import capture_agent_vm
                conversation=capture_agent_vm(self,conversation,conversation_row,agent_vm)
                captured_vm=conversation.agent_vm
            from .behavior_conversation_transfer import current_transfer
            environment_transfer=current_transfer(self,conversation)
            from .behavior_profile_selection import choice_for_conversation,head_key,selection_key,profile_from_template
            profile_choice=self.store.consistent_read(lambda:choice_for_conversation(self,conversation))
            effective_profile=policy.profile if profile_choice is None else profile_from_template(policy,profile_choice.command.template)
            profile_reads=(() if conversation.jobs else (head_key(policy.authority_domain,policy.agent_id),)) + (
                () if profile_choice is None else (selection_key(policy.authority_domain,profile_choice.selection_id,policy.agent_id),))
            profile_snapshot=self.store.read_snapshot(profile_reads)
            captured=self.store.consistent_read(lambda:self.conversation_memory(principal,conversation))
            memory_capture,memory_snapshot=captured.value,captured.predicates
            if not conversation.jobs:
                from .behavior_profile_selection import ConversationProfileHead
                row=profile_snapshot[head_key(policy.authority_domain,policy.agent_id)]
                selected=None if row is None else _parse(row.payload["data"],ConversationProfileHead).selection_id
                if selected!=(None if profile_choice is None else profile_choice.selection_id):
                    raise Problem(409,"PROFILE_SELECTION_CONFLICT","the new-conversation profile changed during admission")
            capability_capture=None
            if effective_profile.capabilities:
                from .behavior_capabilities import capture as capture_capabilities,runtime_grants
                from .behavior_profile_selection import ConversationProfileHead,current_selection
                if profile_choice is None:
                    raise Problem(403,"CAPABILITY_SELECTION_REQUIRED","capabilities apply only through a human profile selection")
                head=profile_choice
                if conversation.jobs:
                    head=self.store.consistent_read(lambda:current_selection(self.store,policy))
                    extra=(head_key(policy.authority_domain,policy.agent_id),)+(
                        () if head is None or head.selection_id==profile_choice.selection_id
                        else (selection_key(policy.authority_domain,head.selection_id,policy.agent_id),))
                    profile_reads=tuple(dict.fromkeys((*profile_reads,*extra)))
                    profile_snapshot=self.store.read_snapshot(profile_reads)
                    row=profile_snapshot[head_key(policy.authority_domain,policy.agent_id)]
                    if (None if row is None else _parse(row.payload["data"],ConversationProfileHead).selection_id)!=(
                            None if head is None else head.selection_id):
                        raise Problem(409,"PROFILE_SELECTION_CONFLICT","the current profile selection changed during admission")
                head_refs=() if head is None else profile_from_template(policy,head.command.template).capabilities
                capability_capture=capture_capabilities(effective_profile.capabilities,head_refs,
                    None if head is None else head.selection_id,runtime_grants(self.runtime))
            if profile_choice is not None:
                prompt_override=None
            elif conversation.jobs:
                previous=self._job_record(conversation.jobs[-1])
                if previous is None:
                    self.refresh()
                    previous=self._job_record(conversation.jobs[-1])
                if (previous is None or previous.profile.instruction is None
                        or previous.profile.instruction.conversation_id!=conversation.conversation_id
                        or previous.profile.instruction.turn_number!=len(conversation.jobs)):
                    raise Problem(409,"CONVERSATION_CONTEXT_CHANGED","original conversation profile is unavailable")
                prompt_override=registered_prompt_override(policy,previous.profile)
            else:
                prompt_override=configured_prompt_override(policy)
            from .behavior_attachment_source import validate_admission
            validate_admission(self,policy.model_copy(update={"profile":effective_profile}),principal,instruction,conversation)
            if conversation_row is not None and captured_vm is None and self.runtime.mode=="native":
                from .behavior_foundry import instruction_target
                executor=self._session_executor()
                target=instruction_target(self.runtime.dep.native_launches.targets[0],binding.job_id,
                    agent_version=conversation.hosted_agent_version,session_id=conversation.hosted_session_id)
                original=executor.clients[target.executor_id]
                client=original if original.target==target else original.for_target(target)
                client.verify_session(before_send=lambda:self._human(principal,"submit"))
            selected_model=self.models.select(instruction.model)
            if capability_capture is not None and capability_capture.vision and selected_model.supports_image_input is not True:
                raise Problem(422,"CAPABILITY_MODEL_UNSUPPORTED","a selected capability needs a model that accepts images")
            selected_capacity = (None if effective_profile.model_capacity_profile is None
                                 else selected_model.capacity_snapshot())
            from .behavior_capability_prompt import skill_access
            if (effective_profile.skill_catalog_budget_profile is not None
                    and skill_access(effective_profile.prompt_profile,capability_capture)):
                from .behavior_skill_catalog import validate_catalog_budget
                if selected_capacity is None:
                    raise Problem(503,"SKILL_CATALOG_CAPACITY_UNKNOWN","catalog budgeting requires declared model capacity")
                validate_catalog_budget(selected_capacity,len(effective_profile.skill_artifacts),self.runtime.dep.memory._count)
            from .behavior_work_target import instruction_hosted_version
            hosted_version=(captured_vm.hosted_agent_version if captured_vm is not None else
                            instruction_hosted_version(self,conversation,existing=conversation_row is not None))
            conversation=conversation.model_copy(update={"hosted_agent_version":hosted_version})
            conversation_reads=(conversation_key(conversation.conversation_id),)
            # Each turn captures the head at its own admission; later versions apply from the next turn on.
            from .behavior_agent_role_prompt_versions import captured_role_prompt
            role_prompt=self.store.consistent_read(
                lambda:captured_role_prompt(self.store,policy,effective_profile.prompt_profile))
            admission,binding,profile=derive_work(policy,request_id,self.runtime.mode,instruction=instruction,
                turn_number=len(conversation.jobs)+1,previous_archive_id=conversation.archive_id,model=selected_model.id,
                hosted_agent_version=hosted_version,model_capacity=selected_capacity,prompt_override=prompt_override,
                reasoning_summary=("detailed" if effective_profile.model_provider=="openai-responses"
                                   and selected_model.supports_reasoning_summary is True else None),
                profile_template=None if profile_choice is None else profile_choice.command.template,
                memory_capture=memory_capture,capability_capture=capability_capture,role_prompt=role_prompt)
        elif peer_work is not None:
            from .behavior_agent_peer_work import peer_reads, narrow_profile
            from .behavior_instruction import Conversation, conversation_key
            peer_snapshot=self.store.read_snapshot(peer_reads(peer_root))
            route=peer_root.directory.agents[1]
            conversation=Conversation(conversation_id=route.conversation_id,human_oid=policy.template.human_oid,
                team_id=policy.template.team_id,agent_oid=policy.template.agent_oid,sdk_session_id=route.sdk_session_id,
                hosted_session_id=route.hosted_session_id,hosted_agent_version=peer_root.recipient_hosted_version)
            conversation_reads=(conversation_key(route.conversation_id),)
            conversation_row=self.store.read_snapshot(conversation_reads)[conversation_reads[0]]
            if conversation_row is not None:
                raise Problem(409,"AGENT_MESSAGE_HANDOFF_EXISTS","this root already has its original receiver conversation")
            selected_model=self.models.select(instruction.model)
            effective_profile=narrow_profile(policy.profile)
            selected_capacity=None if effective_profile.model_capacity_profile is None else selected_model.capacity_snapshot()
            admission,binding,profile=derive_work(policy,request_id,self.runtime.mode,instruction=instruction,
                model=selected_model.id,hosted_agent_version=peer_root.recipient_hosted_version,
                model_capacity=selected_capacity,peer_work=peer_work,
                reasoning_summary=("detailed" if effective_profile.model_provider=="openai-responses"
                                   and selected_model.supports_reasoning_summary is True else None))
        observation=self.cost_reader()
        if instruction is not None:
            # Before the first write (the cost observation below): the later dispatch row must fit one Table row.
            check_dispatch_row(self,admission,binding,profile,
                self._per_work_policy(policy,admission,binding,profile,self.runtime._now(),True),observation)
        self.observe_cost(observation,instruction=instruction is not None)
        rows=self.store.read_snapshot((self.policy_key,self.revocation_key,self.cost_key,self.registry_key))
        policy=self.installed(rows)
        decision=cumulative_decision(policy,_row(rows,self.cost_key,CostLedger),observation,self.runtime._now(),
                                     read_id=request_id,mode=self.runtime.mode,instruction=instruction is not None)
        if decision.decision!="allow":
            raise Problem(403,"WORK_POLICY_"+decision.reason.upper(),"new paid work is not authorized")
        if instruction is None:self.source_ready(principal,policy)
        now=self.runtime._now()
        work_policy=self._per_work_policy(policy,admission,binding,profile,now,instruction is not None)
        registered=WorkRegistration(admission_id=admission,request_id=request_id,submit_sha256=submit_sha256,
            policy_id=policy.policy_id,policy_revision=policy.revision,policy_sha256=policy.sha256,
            principal_oid=principal.oid,binding=binding,profile=profile,runtime_policy=work_policy,
            hosted_session_id=None if conversation is None else conversation.hosted_session_id,
            profile_selection_id=None if profile_choice is None else profile_choice.selection_id,
            environment_transfer_id=None if environment_transfer is None else environment_transfer.transfer_id,
            agent_scope=policy.agent_scope,routine=routine,
            turn_authorization=None if instruction is None else TurnAuthorization(),agent_vm=captured_vm)
        ownerkey=control_key("owner",binding.owner.owner_record_id)
        opkey=control_key("policy",binding.owner.policy_id)
        rpkey=runtime_key("policy",binding.binding_id)
        indexkey=runtime_key("plan-index",binding.binding_id)
        grant_writes=()
        target_reads=()
        target_row=None
        if self.runtime.mode=="native":
            import hashlib
            from .behavior_foundry import NativeExecutionGrant,native_key,instruction_target
            from .behavior_assistant_wire import Task
            native=self.runtime.dep.native_launches
            if native is None:raise Problem(403,"WORK_POLICY_NATIVE_DISABLED","existing original native authority required")
            from .behavior_work_target import target_key, StandingTarget
            selection_key=target_key(policy.authority_domain,policy.agent_id)
            target_reads=(selection_key,)
            target_row=self.store.read_snapshot(target_reads)[selection_key]
            if target_row is not None:
                selected=_parse(target_row.payload["data"],StandingTarget)
                if native.targets!=(selected.command.target,):
                    raise Problem(409,"STANDING_TARGET_DENIED","reload the selected native target before new admission")
            task=Task(job_id=binding.job_id,work_id=binding.work.work_id,
                definition_digest=digest(binding.work.model_dump(mode="json")),profile_digest=digest(profile.model_dump(mode="json")),
                source=profile.source,artifact_id=profile.artifact_id,artifact_version=profile.artifact_version,
                operation_id=binding.operation_id,instruction=profile.instruction)
            gid=native_key("bootstrap-grant",binding.job_id)
            grant=NativeExecutionGrant(grant_id=gid,controller_id=native.controller_id,owner=binding.owner,
                policy_revision=work_policy.operation_policy_revision,job_id=binding.job_id,definition=binding.work,
                input_digest=hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
                targets=tuple(instruction_target(t,binding.job_id,agent_version=profile.instruction.hosted_agent_version,
                                                session_id=registered.hosted_session_id)
                              for t in native.targets) if instruction is not None else native.targets,
                active=True,remaining_uses=1,not_before=now,expires_at=work_policy.queue_deadline,workload_version="assistant-v1.2")
            grant_writes=(_write(native_key("grant",gid),grant,"create"),)
        request=ControlRequest(request_id=key,request_digest=registration_request_digest(registered),
            read_set=tuple(dict.fromkeys((self.policy_key,self.revocation_key,self.cost_key,self.registry_key,key,
                      ownerkey,opkey,rpkey,indexkey,*target_reads,*profile_reads,*conversation_reads,*memory_snapshot,*peer_snapshot))))
        def changes(current):
            from .behavior_work_registry import append_writes, count_registry
            active=self.installed(current)
            if routine is not None:
                validate_submission(active,principal.oid,instruction,routine,now=self.runtime._now())
            if target_reads and current[target_reads[0]]!=target_row:
                raise Problem(412,"BEHAVIOR_CONFLICT","selected target changed before original registration")
            if conversation_reads and current[conversation_reads[0]]!=conversation_row:
                raise Problem(412,"BEHAVIOR_CONFLICT","original conversation changed before admission")
            if conversation is not None and conversation.jobs and any(
                    current[name]!=value for name,value in admission_registry.items()):
                raise Problem(412,"BEHAVIOR_CONFLICT","the continuation registry changed before admission")
            if any(current[key]!=value for key,value in profile_snapshot.items()):
                raise Problem(412,"BEHAVIOR_CONFLICT","approved profile selection changed before admission")
            if any(current[key]!=value for key,value in memory_snapshot.items()):
                raise Problem(412,"BEHAVIOR_CONFLICT","memory setting or selected note changed before admission")
            if any(current[key]!=value for key,value in peer_snapshot.items()):
                raise Problem(412,"BEHAVIOR_CONFLICT","the original peer assignment changed before registration")
            if peer_root is not None:
                from .behavior_delegation import _current_parent
                _current_parent(current,peer_root.origin)
            if active!=policy or any(current[k] is not None for k in (key,ownerkey,opkey,rpkey,indexkey)):
                raise Problem(409,"WORK_POLICY_SCOPE_CHANGED","exact new registration changed")
            if instruction is None and count_registry(self.store,policy,current)>=32:
                raise Problem(429,"WORK_POLICY_CATALOG_CAPACITY","bounded retained registry capacity is exhausted")
            from .behavior_instruction import registration_write
            conversation_writes=() if conversation is None else (registration_write(conversation,binding.job_id),)
            return ControlChanges(writes=(
                _write(key,registered,"create",material(registered.model_dump(mode="json"))),
                _write(ownerkey,OwnerRecord(**binding.owner.model_dump(),active=True,operation_ids=(binding.operation_id,)),"create"),
                _write(opkey,PolicyRecord(policy_id=binding.owner.policy_id,revision=work_policy.operation_policy_revision,active=True),"create"),
                _write(rpkey,work_policy,"create"),_write(indexkey,_PlanIndex(),"create"),
                *append_writes(policy,current,admission),
                *grant_writes,*conversation_writes,
            ),result={"admission_id":admission,"binding_id":binding.binding_id,"not_yet_admitted":True})
        from .behavior_work_registry import transact
        transact(self.store,policy,request,changes,append=True)
        self.records[registered.binding.binding_id]=registered
        self.refresh(registered.binding.binding_id)
        return registered

    def _per_work_policy(self,policy,admission,binding,profile,now,instruction):
        fields=policy.legacy_template_policy.model_dump()
        from .memory_bridge_settings import captured_policy_fields
        fields.update(captured_policy_fields(profile.memory_scope))
        fields.update(binding_digest=self.runtime.binding_digest(binding),valid_until=now+timedelta(seconds=policy.queue_seconds),
            schema_version="fa-per-work-runtime-policy-v1",policy_id=policy.policy_id,policy_revision=policy.revision,
            admission_id=admission,job_id=binding.job_id,stage="queued",accepted_at=now,
            queue_deadline=now+timedelta(seconds=policy.queue_seconds),dispatched_at=None)
        if instruction:
            fields.update(instruction=True,valid_until=None,queue_deadline=None)
        return PerWorkRuntimePolicy(**fields)

    def observe_cost(self,observation,*,instruction=False):
        self._runtime_scope()
        rows=self.store.read_snapshot((self.policy_key,self.revocation_key,self.cost_key))
        policy=self.installed(rows);ledger=_row(rows,self.cost_key,CostLedger)
        decision=cumulative_decision(policy,ledger,observation,self.runtime._now(),read_id="cost-observation",
                                     mode=self.runtime.mode,instruction=instruction)
        if decision.reason not in {"allowed","cumulative_budget_exhausted"}:
            raise Problem(403,"WORK_POLICY_"+decision.reason.upper(),"cost observation cannot authorize new paid work")
        observed_sha=digest(observation.model_dump(mode="json"))
        if ledger.latest_observation_sha256==observed_sha:return
        record=CostObservationUpdate(policy_id=policy.policy_id,observation=observation,
                                     recorded_at=self.runtime._now(),instruction=instruction)
        key=work_policy_key("cost-observation",policy.authority_domain,observed_sha)
        request=ControlRequest(request_id=key,request_digest=observed_sha,
            read_set=(self.policy_key,self.revocation_key,self.cost_key,key))
        def decide(current):
            active=self.installed(current);previous=_row(current,self.cost_key,CostLedger)
            check=cumulative_decision(active,previous,observation,self.runtime._now(),read_id="cost-observation",
                                      mode=self.runtime.mode,instruction=instruction)
            if check.reason not in {"allowed","cumulative_budget_exhausted"}:
                raise Problem(403,"WORK_POLICY_"+check.reason.upper(),"cumulative observation regressed")
            updated=previous.model_copy(update={
                "observed_high_water_microusd":max(previous.observed_high_water_microusd,observation.observed_cumulative_microusd),
                "legacy_unresolved_reserve_microusd":max(previous.legacy_unresolved_reserve_microusd,observation.legacy_unresolved_reserve_microusd),
                "latest_observed_at":observation.observed_at,"latest_observation_sha256":observed_sha})
            return ControlChanges(writes=(_write(self.cost_key,updated,"replace"),
                _write(key,record,"create",material(record.model_dump(mode="json")))),
                result={"observation_sha256":observed_sha,"reservation_added":False})
        self.store.transact(request,decide)

    def refresh(self,binding_id=None):
        from types import MappingProxyType
        from .behavior_work_registry import head_key,read_registry
        self._runtime_scope()
        done=_REFRESHED.get()
        phase_key=(id(self),binding_id)
        if done is not None and binding_id is not None and phase_key in done and binding_id in self.records:
            return
        # Taken before the registry read: anything cached now was registered before that index.
        seen=set(self.records)
        view=_READ_VIEW.get()
        if view is not None:
            # A pure-read view returns its first registry copy to every later refresh; only work cached
            # before that copy was read may be dropped by it, never work another thread admitted since.
            marks=getattr(view,"work_policy_seen",None)
            if marks is None:
                marks={}
                view.work_policy_seen=marks
            seen=marks.setdefault(id(self),seen)
        rows=self.store.read_snapshot((self.policy_key,self.revocation_key,self.registry_key,head_key(self.registry_key)))
        self.installed(rows)
        index=read_registry(self.store,self.policy,rows)
        if index.policy_id!=self.policy.policy_id:
            raise Problem(409,"WORK_POLICY_REGISTRY_INVALID","work registry belongs to another agent policy")
        cached=self.records.get(binding_id)
        scoped=cached is not None and cached.admission_id in index.entries
        admitted={k:r for k,r in self.records.items() if r.turn_authorization is not None}
        records={**admitted,**({k:r for k,r in self.records.items() if r.admission_id in index.entries} if scoped else {})}
        for admission in ((cached.admission_id,) if scoped else index.entries):
            key=work_policy_key("registration",admission)
            row=self.store.read_snapshot((key,))[key]
            if row is None:raise Problem(409,"WORK_POLICY_REGISTRY_INVALID","original registered work is missing")
            record=_parse(row.payload["data"],WorkRegistration)
            turn=record.profile.instruction
            from .behavior_conversation_transfer import registration_transfer
            transfer=registration_transfer(self,record)
            if record.hosted_session_id is not None:
                from .behavior_instruction import hosted_session_id
                if turn is None or transfer is None and record.agent_vm is None \
                        and record.hosted_session_id!=hosted_session_id(turn.conversation_id):
                    raise Problem(409,"WORK_POLICY_REGISTRY_INVALID","Hosted session is outside this original conversation")
            from .behavior_profile_selection import read_selection
            choice=(None if record.profile_selection_id is None
                    else read_selection(self.store,self.policy,record.profile_selection_id))
            if choice is not None:
                self.profile_selections[choice.selection_id]=choice
            if record.profile.memory_scope is not None:
                from .memory_bridge_settings import read_memory_settings, validate_capture_record
                setting=read_memory_settings(self.store,self.policy,record.profile.memory_scope.settings_id)
                validate_capture_record(self.policy,record.profile.memory_scope,setting)
            derived,binding,profile=derive_registered_work(self.policy,record,self.runtime.mode,
                profile_template=None if choice is None else choice.command.template)
            if (derived!=admission or record.binding!=binding or record.profile!=profile
                    or turn is not None and (None if turn.model_capacity is None else turn.model_capacity.profile)
                    != record.profile.model_capacity_profile
                    or row.payload["material"]!=material(record.model_dump(mode="json")).model_dump(mode="json")):
                raise Problem(409,"WORK_POLICY_REGISTRY_INVALID","registered original work differs")
            records[binding.binding_id]=record
        with _REFRESH_LOCK:
            # An admitted turn keeps its original identity; this index is checked again at the next admission.
            records={**{k:r for k,r in self.records.items()
                        if k not in seen or r.admission_id in index.entries or r.turn_authorization is not None},
                     **records}
            self.records=records
            self.runtime.bindings=MappingProxyType({**self.base_bindings,**{k:r.binding for k,r in records.items()}})
            self.runtime.profiles=MappingProxyType({**self.base_profiles,**{k:r.profile for k,r in records.items()}})
        if self.runtime.dep.native_launches is not None:
            from .behavior_foundry import NativeJobRoute,native_key
            for record in records.values():
                b=record.binding
                self.runtime.dep.native_launches.jobs[b.job_id]=NativeJobRoute(job_id=b.job_id,api_binding_id=b.binding_id,
                    grant_id=native_key("bootstrap-grant",b.job_id))
        if done is not None and binding_id is not None:
            done.add(phase_key)

    def listed(self,binding_id,rows=None):
        """Whether the current registry still lists this cached work.

        The CAS gate re-reads the registration row but not the registry, so a light route check
        uses this instead of a full refresh; ``rows`` may already hold the registry index and head.
        """
        from .behavior_work_registry import read_registry
        record=self.records.get(binding_id)
        return record is not None and record.admission_id in read_registry(self.store,self.policy,rows).entries

    def gate_keys(self,binding):
        if binding.binding_id not in self.records:return ()
        record=self.records[binding.binding_id]
        from .behavior_profile_selection import selection_key
        peer_keys=()
        if record.profile.instruction is not None and record.profile.instruction.peer_work is not None:
            from .behavior_agent_message_api import _read_root
            from .behavior_agent_peer_work import peer_reads
            root_id=record.profile.instruction.peer_work.root_id
            root=self.peer_roots.get(root_id)
            if root is None:
                root=_read_root(self.store,root_id)
                self.peer_roots[root_id]=root
            peer_keys=peer_reads(root)
        return (self.policy_key,self.revocation_key,work_policy_key("registration",record.admission_id),
                *((selection_key(self.policy.authority_domain,record.profile_selection_id,
                                 None if record.agent_scope is None else record.agent_scope.agent_id),)
                  if record.profile_selection_id is not None else ()),
                *((record.profile.memory_scope.settings_id,) if record.profile.memory_scope is not None else ()),
                *peer_keys)

    def check(self,binding,policy,rows,*,use):
        current=self.installed(rows)
        record=self.records.get(binding.binding_id)
        if (record is None or record.binding!=binding or policy.policy_id!=current.policy_id
                or policy.admission_id!=record.admission_id or policy.job_id!=binding.job_id
                or policy.instruction!=(record.profile.instruction is not None)
                or not current.active or use=="read" and not current.read_enabled):
            raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","current original work/read scope changed")
        key=work_policy_key("registration",record.admission_id)
        actual=_row(rows,key,WorkRegistration)
        if (actual!=record or rows[key].payload["material"]!=material(actual.model_dump(mode="json")).model_dump(mode="json")):
            raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","current transaction registration differs")
        if record.profile.instruction is not None and record.profile.instruction.peer_work is not None:
            from .behavior_agent_message_api import AgentCollaborationRoot
            from .behavior_agent_peer_work import validate_reference
            from .behavior_delegation import _current_parent
            peer_work=record.profile.instruction.peer_work
            root=_row(rows,peer_work.root_id,AgentCollaborationRoot)
            if root!=self.peer_roots.get(peer_work.root_id):
                raise Problem(403,"AGENT_MESSAGE_ASSIGNMENT_DENIED","the immutable original peer root changed")
            validate_reference(root,current,peer_work,record.request_id,registered_instruction(record))
            _current_parent(rows,root.origin,executing=use not in {"read","control"})
        if record.profile_selection_id is not None:
            from .behavior_profile_selection import ConversationProfileSelection,selection_key
            choicekey=selection_key(self.policy.authority_domain,record.profile_selection_id,
                                    None if record.agent_scope is None else record.agent_scope.agent_id)
            choice=_row(rows,choicekey,ConversationProfileSelection)
            if (self.profile_selections.get(record.profile_selection_id)!=choice
                    or rows[choicekey].payload["material"]!=material(choice.model_dump(mode="json")).model_dump(mode="json")):
                raise Problem(403,"PROFILE_SELECTION_INVALID","the original approved profile selection changed")
        if record.profile.memory_scope is not None:
            from .memory_bridge_settings import MemoryBridgeSettingsRecord, validate_capture_record
            memory_key=record.profile.memory_scope.settings_id
            setting=_row(rows,memory_key,MemoryBridgeSettingsRecord)
            validate_capture_record(self.policy,record.profile.memory_scope,setting)
            if rows[memory_key].payload["material"]!=material(setting.model_dump(mode="json")).model_dump(mode="json"):
                raise Problem(403,"MEMORY_SETTINGS_INVALID","the original memory setting changed")
        if not policy.instruction and use not in {"read","control"} and self.runtime._now()>=policy.valid_until:
            raise Problem(403,"WORK_EXECUTION_DEADLINE","only this work's execution permission has expired")

    def verify_result_access(self,access):
        from . import behavior_delivery as delivery
        from .behavior_inbox import CurrentWorkInboxAccess
        from .behavior_foundry import _NativeLaunch,native_key
        from .behavior_workers import ExecutorEvidence
        if type(access) is not CurrentWorkInboxAccess:
            raise Problem(403,"WORK_RESULT_ACCESS_DENIED","explicit current result access required")
        # Refresh only this reader: this check also runs inside the fetch EGT decision window.
        self.refresh(access.runtime_binding_id)
        record=self.records.get(access.runtime_binding_id)
        if record is None:
            raise Problem(403,"WORK_RESULT_ACCESS_DENIED","registered original result reader required")
        b=record.binding
        keys=(*self.runtime._gate_keys(b),control_key("delivery-work-state",b.obligation_id),
              worker_key("job",b.job_id),control_key("delivery-obligation",b.obligation_id),
              control_key("delivery-outbox",access.binding.delivery_id))
        rows=self.store.read_snapshot(keys)
        policy,context=self.runtime._state(b,rows,use="read")
        job=_row(rows,worker_key("job",b.job_id),JobRecord)
        work=_row(rows,control_key("delivery-work-state",b.obligation_id),delivery._Work)
        ob=_row(rows,control_key("delivery-obligation",b.obligation_id),delivery._Obligation)
        out=_row(rows,control_key("delivery-outbox",access.binding.delivery_id),delivery._Outbox)
        result=work.result
        if (access.work_policy_id!=policy.policy_id or access.principal_id!=b.human_oid or access.binding!=out.binding
                or access.authentication_version!=self.runtime.dep.evidence.authentication_version
                or access.policy_revision!=policy.operation_policy_revision
                or job.state not in {"succeeded","failed","cancelled"} or not job.result_forwarded or job.completion_credit
                or work.definition!=b.work or work.state!=job.state or result is None
                or result.work_id!=b.work.work_id or result.state!=job.state
                or result.definition_digest!=digest(b.work.model_dump(mode="json")) or result.work_revision!=1
                or result.verifier_version!=self.runtime.dep.delivery.contract.verifier_version
                or job.spec.job_id!=b.job_id or job.spec.work_id!=b.work.work_id or job.spec.obligation_id!=b.obligation_id
                or job.spec.input!=self.runtime.job_input(b)
                or ob.route!=b.route or ob.final_delivery_id!=access.binding.delivery_id
                or out.binding.route!=b.route or out.binding.obligation_id!=b.obligation_id
                or any(getattr(job.owner,k)!=getattr(context,k) for k in type(context).model_fields if k!="epoch")):
            raise Problem(403,"WORK_RESULT_ACCESS_DENIED","only the exact saved terminal result is readable")
        resultkey=worker_key("work-result",result.evidence_id)
        saved=self.store.read_snapshot((resultkey,))[resultkey]
        if (saved is None or _parse(saved.payload["data"],delivery.VerifiedWorkResult)!=result
                or saved.payload["material"]!=material(result.model_dump(mode="json")).model_dump(mode="json")):
            raise Problem(403,"WORK_RESULT_ACCESS_DENIED","original worker result material differs")
        kind="final" if result.state=="succeeded" else "failure"
        expected=delivery._Payload(event_id=b.event_id,root_request_id=b.root_request_id,work_id=b.work.work_id,
            message_kind=kind,work_state=result.state,result_evidence_id=result.evidence_id,content=result.content,
            artifacts=result.artifacts,known_effects=result.known_effects,unresolved_operations=result.unresolved_operations,
            responsibility_owner=b.owner.owner_record_id,notification_code="verified_result" if kind=="final" else "delivery_needs_attention")
        payload=material(expected.model_dump(mode="json"))
        if (out.message_kind!=kind or out.binding.payload_digest!=payload.sha256 or out.binding.artifacts!=result.artifacts
                or rows[control_key("delivery-outbox",access.binding.delivery_id)].payload["material"]!=payload.model_dump(mode="json")):
            raise Problem(403,"WORK_RESULT_ACCESS_DENIED","exact retained payload/route differs")
        if self.runtime.mode=="native":
            if job.binding is None:
                from .behavior_workers import _InactiveCancellation
                completionkey=worker_key("completion",b.job_id,"0")
                never_started=self.store.read_snapshot((completionkey,
                    worker_key("attempt",b.job_id,"1"),
                    native_key("launch",native_key("launch-id",worker_key("start",b.job_id,"1")))))
                evidence=_row(never_started,completionkey,_InactiveCancellation)
                if (job.state!="cancelled" or job.generation!=0 or not job.cancel_requested
                        or evidence.spec!=job.spec or evidence.owner!=job.owner or evidence.generation!=0
                        or evidence.binding is not None or evidence.prior_evidence_id is not None or evidence.children
                        or evidence.evidence_id!=job.last_evidence_id
                        or any(value is not None for key,value in never_started.items() if key!=completionkey)
                        or never_started[completionkey].payload["material"]!=material(evidence.model_dump(mode="json")).model_dump(mode="json")):
                    raise Problem(403,"WORK_RESULT_ACCESS_DENIED","only an original fenced never-started cancellation is eligible")
                return
            launchkey=native_key("launch",native_key("launch-id",job.binding.start_request_id))
            completionkey=worker_key("completion",b.job_id,str(job.generation))
            native=self.store.read_snapshot((launchkey,completionkey))
            launch=_row(native,launchkey,_NativeLaunch);completed=_row(native,completionkey,ExecutorEvidence)
            if (launch.phase!="sealed" or completed.binding!=job.binding or completed.result!=result
                    or launch.start.binding!=job.binding or launch.api_binding_id!=b.binding_id or launch.definition!=b.work
                    or launch.configuration_digest!=self.runtime.dep.workers.configuration_digest()
                    or completed.state!=("stopped" if job.state=="cancelled" else job.state)
                    or not completed.entire_tree_idle or not completed.all_inflight_starts_fenced
                    or completed.evidence_id!=job.last_evidence_id or completed.native_execution_id!=launch.native_invocation_id
                    or native[completionkey].payload["material"]!=material(completed.model_dump(mode="json")).model_dump(mode="json")):
                raise Problem(403,"WORK_RESULT_ACCESS_DENIED","saved result lacks original terminal native proof")

    def _admission_record(self,command):
        return next((record for record in self.records.values() if record.binding.event_id==command.event.event_id),None)

    def admission_keys(self,command):
        record=self._admission_record(command)
        if record is None:return ()
        return tuple(dict.fromkeys((
            self.policy_key,self.revocation_key,self.cost_key,work_policy_key("registration",record.admission_id),
            work_policy_key("reservation",record.admission_id),runtime_key("policy",record.binding.binding_id),
            *self.gate_keys(record.binding),
        )))

    def admission_observation(self,command):
        return None if self._admission_record(command) is None else self.cost_reader()

    def admission_writes(self,rows,command,context,actor,observation):
        record=self._admission_record(command)
        if record is None:return ()
        current=_row(rows,work_policy_key("registration",record.admission_id),WorkRegistration)
        policy=self.installed(rows);b=current.binding
        runtime_policy=_row(rows,runtime_key("policy",b.binding_id),PerWorkRuntimePolicy)
        self.check(b,runtime_policy,rows,use="execute")
        if (current!=record or record.policy_sha256!=policy.sha256 or b.owner!=context
                or command.work!=b.work or command.route!=b.route or command.event.root_request_id!=b.root_request_id
                or actor.source_kind!=("routine" if record.routine is not None else "human")
                or actor.actor_id!="human-"+b.human_oid):
            raise Problem(403,"WORK_POLICY_ADMISSION_SCOPE","same original human/task/route required")
        ledger=_row(rows,self.cost_key,CostLedger)
        instruction=record.profile.instruction is not None
        decision=cumulative_decision(policy,ledger,observation,self.runtime._now(),
                                     read_id=record.request_id,mode=self.runtime.mode,instruction=instruction)
        if decision.decision!="allow":
            raise Problem(403,"WORK_POLICY_"+decision.reason.upper(),"current cumulative budget does not allow new paid work")
        if instruction:return ()
        root=_row(rows,control_key("budget",context.root_budget_id),BudgetRecord)
        if (root.remaining_attempts<policy.max_model_requests+3
                or root.remaining_units<policy.max_model_requests+2+self.runtime.dep.workers.contract.reservation_units):
            raise Problem(429,"WORK_POLICY_CONTROL_BUDGET","original abstract model/artifact budget is insufficient")
        reservation_key=work_policy_key("reservation",record.admission_id)
        if rows[reservation_key] is not None:
            raise Problem(409,"WORK_POLICY_RESERVATION_WITHOUT_ADMISSION","reservation alone cannot stand for an admitted work")
        reserved=WorkCostReservation(policy_id=policy.policy_id,policy_sha256=policy.sha256,
            admission_id=record.admission_id,job_id=b.job_id,root_budget_id=context.root_budget_id,
            estimate_microusd=policy.work_estimate_microusd,observation=observation,reserved_at=self.runtime._now())
        updated=ledger.model_copy(update={
            "observed_high_water_microusd":max(ledger.observed_high_water_microusd,observation.observed_cumulative_microusd),
            "legacy_unresolved_reserve_microusd":max(ledger.legacy_unresolved_reserve_microusd,observation.legacy_unresolved_reserve_microusd),
            "work_reservations_microusd":ledger.work_reservations_microusd+reserved.estimate_microusd,
            "reservation_count":ledger.reservation_count+1,"latest_observed_at":observation.observed_at,
            "latest_observation_sha256":digest(observation.model_dump(mode="json"))})
        return (_write(self.cost_key,updated,"replace"),_write(reservation_key,reserved,"create",material(reserved.model_dump(mode="json"))))

    def admission_commit(self,command,observation,commit):
        if self._admission_record(command) is None:return commit()
        def final():
            if self.cost_reader()!=observation:
                raise Problem(412,"WORK_POLICY_COST_CHANGED","current cost observation changed before admission")
            rows=self.store.read_snapshot((self.policy_key,self.revocation_key,self.cost_key))
            decision=cumulative_decision(self.installed(rows),_row(rows,self.cost_key,CostLedger),observation,
                self.runtime._now(),read_id=self._admission_record(command).request_id,mode=self.runtime.mode,
                instruction=self._admission_record(command).profile.instruction is not None)
            if decision.decision!="allow":
                raise Problem(403,"WORK_POLICY_"+decision.reason.upper(),"last admission permission denied")
        if self.runtime.mode=="native":
            from .behavior_migration import MigrationStore
            if type(self.store) is not MigrationStore:
                raise Problem(403,"WORK_POLICY_NATIVE_DISABLED","native final physical guard is missing")
            return self.store.validated_writes(commit,final)
        return commit()

    def _job_record(self,job_id):
        return next((r for r in self.records.values() if r.binding.job_id==job_id),None)

    def _session_executor(self):
        from .behavior_foundry import FoundryExecutor
        executor=getattr(self.runtime.dep.workers.adapter.start,"__self__",None)
        if type(executor) is not FoundryExecutor or executor.authority is not self.runtime.dep.native_launches:
            raise Problem(503,"INSTRUCTION_HOSTED_CONFIGURATION","the original concrete session executor is required")
        return executor

    def reset_conversation(self,principal,identifier,command):
        from .behavior_instruction import archive_conversation
        receipt=archive_conversation(self,principal,identifier,command)
        result=receipt.payload["result"]
        state="not_run"
        if self.runtime.mode=="native" and result["job_id"] is not None:
            from .behavior_foundry import native_key,InstructionSessionStop
            job=self.runtime.dep.workers.inspect(result["job_id"])
            if job.binding is not None:
                executor=self._session_executor()
                launch=executor.authority.launch(native_key("launch-id",job.binding.start_request_id))
                executor._cleanup_instruction_session(launch,retire=True)
                key=native_key("instruction-session-stop",launch.launch_id)
                row=self.store.read_snapshot((key,))[key]
                stopped=None if row is None else _parse(row.payload["data"],InstructionSessionStop)
                state="confirmed" if stopped is not None and stopped.confirmed else "unknown"
        return {**result,"session_stop":state}

    def start_keys(self,job_id):
        record=self._job_record(job_id)
        if record is None:return ()
        from .behavior_foundry import native_key
        return (self.policy_key,self.revocation_key,self.cost_key,work_policy_key("reservation",record.admission_id),
                work_policy_key("registration",record.admission_id),runtime_key("policy",record.binding.binding_id),
                work_policy_key("dispatch",record.admission_id),
                *((native_key("grant",native_key("bootstrap-grant",job_id)),) if self.runtime.mode=="native" else ()))

    def start_observation(self,job_id):
        return None if self._job_record(job_id) is None else self.cost_reader()

    def _dispatch_budget(self,rows,record,observation):
        policy=self.installed(rows)
        ledger=_row(rows,self.cost_key,CostLedger)
        instruction=record.profile.instruction is not None
        if not instruction:
            held=_row(rows,work_policy_key("reservation",record.admission_id),WorkCostReservation)
            if (held.job_id!=record.binding.job_id or held.policy_sha256!=record.policy_sha256
                    or held.estimate_microusd!=policy.work_estimate_microusd or record.policy_sha256!=policy.sha256):
                raise Problem(403,"WORK_POLICY_RESERVATION_SCOPE","this work's original cost reservation differs")
        decision=cumulative_decision(policy,ledger,observation,self.runtime._now(),read_id=record.request_id,
                                     mode=self.runtime.mode,instruction=instruction)
        total=decision.observed_high_water_microusd+decision.reserved_microusd+policy.safety_reserve_microusd
        if (decision.reason not in {"allowed","cumulative_budget_exhausted"}
                or ledger.budget_enforced and total>policy.cumulative_ceiling_microusd):
            raise Problem(403,"WORK_POLICY_"+decision.reason.upper(),"reserved work cannot start under current cumulative facts")
        return policy,ledger

    def start_writes(self,rows,dispatch,observation):
        record=self._job_record(dispatch.binding.job_id)
        if record is None:return ()
        policy,ledger=self._dispatch_budget(rows,record,observation)
        key=runtime_key("policy",record.binding.binding_id)
        queued=_row(rows,key,PerWorkRuntimePolicy)
        now=self.runtime._now()
        instruction=record.profile.instruction is not None
        if ((queued.stage!="queued" if not instruction else queued.stage not in {"queued","dispatched"})
                or not instruction and (dispatch.binding.generation!=1 or dispatch.checkpoint is not None)
                or dispatch.binding.owner!=record.binding.owner
                or not instruction and not queued.accepted_at<=now<queued.queue_deadline):
            raise Problem(403,"WORK_POLICY_START_FENCED","one original queued scope can dispatch only once")
        running=PerWorkRuntimePolicy.model_validate({**queued.model_dump(),"stage":"dispatched","dispatched_at":now,
                                                    "valid_until":None if instruction else now+timedelta(seconds=policy.execution_seconds)})
        permission=WorkDispatchPermission(admission_id=record.admission_id,start=dispatch,policy_after=running,observation=observation)
        permission_key=work_policy_key("dispatch",record.admission_id)
        if rows[permission_key] is not None:
            raise Problem(409,"WORK_POLICY_START_FENCED","this original dispatch permission already exists")
        writes=[_write(key,running,"replace"),*(() if instruction else (_write(self.cost_key,ledger.model_copy(update={
            "observed_high_water_microusd":max(ledger.observed_high_water_microusd,observation.observed_cumulative_microusd),
            "legacy_unresolved_reserve_microusd":max(ledger.legacy_unresolved_reserve_microusd,observation.legacy_unresolved_reserve_microusd),
            "latest_observed_at":observation.observed_at,            "latest_observation_sha256":digest(observation.model_dump(mode="json"))}),"replace"),)),
            _write(permission_key,permission,"create",material(permission.model_dump(mode="json")))]
        if self.runtime.mode=="native":
            from .behavior_foundry import NativeExecutionGrant,native_key
            gkey=native_key("grant",native_key("bootstrap-grant",record.binding.job_id))
            grant=_row(rows,gkey,NativeExecutionGrant)
            if not grant.active or grant.remaining_uses!=1 or grant.job_id!=record.binding.job_id:
                raise Problem(403,"WORK_POLICY_START_FENCED","original one-generation grant changed")
            writes.append(_write(gkey,grant.model_copy(update={"not_before":now,"expires_at":running.valid_until}),"replace"))
        return tuple(writes)

    def start_commit(self,job_id,observation,commit):
        record=self._job_record(job_id)
        if record is None:return commit()
        def check():
            if self.cost_reader()!=observation:
                raise Problem(412,"WORK_POLICY_COST_CHANGED","current dispatch cost observation changed")
            self._dispatch_budget(self.store.read_snapshot(self.start_keys(job_id)),record,observation)
            current=_row(self.store.read_snapshot((runtime_key("policy",record.binding.binding_id),)),
                         runtime_key("policy",record.binding.binding_id),PerWorkRuntimePolicy)
            if current.queue_deadline is not None and self.runtime._now()>=current.queue_deadline:
                raise Problem(403,"WORK_EXECUTION_DEADLINE","queued work expired before acquiring capacity")
            if record.profile.instruction is not None and record.profile.instruction.peer_work is not None:
                from .behavior_agent_message_runtime import check_start
                check_start(self,record)
        if self.runtime.mode=="native":
            return self.store.validated_writes(commit,check)
        return commit()

    def check_dispatch(self,job_id,binding):
        record=self._job_record(job_id)
        if record is None:return
        keys=(*self.runtime._gate_keys(record.binding),worker_key("job",job_id),*self.start_keys(job_id))
        rows=self.store.read_snapshot(tuple(dict.fromkeys(keys)))
        self.runtime._state(record.binding,rows)
        self._dispatch_budget(rows,record,self.cost_reader())
        policy=_row(rows,runtime_key("policy",record.binding.binding_id),PerWorkRuntimePolicy)
        job=_row(rows,worker_key("job",job_id),JobRecord)
        if (policy.stage!="dispatched" or job.binding!=binding or job.cancel_requested
                or job.state not in {"starting","running","capacity_uncertain"}
                or policy.valid_until is not None and self.runtime._now()>=policy.valid_until):
            raise Problem(403,"WORK_POLICY_START_FENCED","current original paid execution permission changed")

    def revoke(self,principal,decision_id):
        self._human(principal,"approve")
        prior=self.store.read_snapshot((self.revocation_key,))[self.revocation_key]
        if prior is not None:
            saved=_parse(prior.payload["data"],WorkPolicyRevocation)
            if saved.decision_id!=decision_id:
                raise Problem(409,"WORK_POLICY_REVOCATION_INVALID","original revocation decision differs")
            return self.store.reconcile(ControlRequest(request_id=self.revocation_key,
                request_digest=digest(saved.model_dump(mode="json")),read_set=(self.policy_key,self.revocation_key)))
        revoked=WorkPolicyRevocation(policy_id=self.policy.policy_id,policy_sha256=self.policy.sha256,
            principal_oid=principal.oid,decision_id=decision_id,revoked_at=self.runtime._now())
        request=ControlRequest(request_id=self.revocation_key,request_digest=digest(revoked.model_dump(mode="json")),
            read_set=(self.policy_key,self.revocation_key))
        def decide(rows):
            self.installed(rows)
            return ControlChanges(writes=(_write(self.revocation_key,revoked,"create",material(revoked.model_dump(mode="json"))),),
                result={"policy_id":self.policy.policy_id,"revoked":True,"reservations_refunded":False})
        return self.store.transact(request,decide)


def attach_standing_policy(composition,*,agent_id=None):
    """Optional exact stored mode; absence leaves every legacy gate intact."""
    runtime=composition.runtime
    if agent_id is not None and agent_id != composition.config.targets[0].agent_id:
        from .behavior_agent_registry import resolve_agent_policy
        policy=resolve_agent_policy(runtime.store,composition.config,agent_id)
    else:
        key=work_policy_key("policy",composition.config.table.scope.authority_domain)
        row=runtime.store.read_snapshot((key,))[key]
        if row is None:return None
        installed=_parse(row.payload["data"],StandingPolicyInstallation)
        policy=installed.policy
    if (policy.configuration_sha256!=composition.config.sha256
            or policy.agent_scope is None and policy.template!=composition.config.bindings[0]
            or policy.cost_observer_oid!=composition.config.evidence.observer_oid):
        raise Problem(403,"WORK_POLICY_SCOPE_CHANGED","stored standing policy does not match the original configuration")
    if runtime.work_policy is not None:
        if runtime.work_policy.policy!=policy:
            raise Problem(503,"AGENT_RUNTIME_SCOPE_NOT_WIRED","one agent's runtime cannot attach another agent's policy")
        runtime.work_policy.refresh()
        return runtime.work_policy
    import os
    from .behavior_server_config import read_document
    def observation():
        path=os.environ.get("FA_WORK_COST_OBSERVATION_FILE")
        return None if path is None else read_document(path,CostObservation)
    reader=getattr(composition,"work_cost_reader",None) or observation
    control=WorkPolicyControl(runtime,policy,reader)
    control.attach()
    return control
