"""Per-request pending/usage estimates in the original USD ledger; no work flat fee."""
import json
import logging
import random
import time
from typing import Annotated, Literal

from pydantic import Field

from .behavior_operations import _Model,_row,_write,_updated,control_key
from .behavior_store import ControlRequest,ControlChanges
from .behavior_assistant_wire import ModelDeployment
from .behavior_turn_usage import TurnModelUsage
from .behavior_work_policy import CostLedger,Money
from .behavior_runtime import material
from .behavior_timing import timing_id
from .models import Identifier
from .source_materials import digest
from .errors import Problem


def cost_key(session_id,request_sha256):
    return control_key("instruction-model-cost",session_id,request_sha256)


def turn_usage_key(job_id,rid):
    """One usage record per original server job and request ID; request IDs alone repeat across Agents."""
    return control_key("instruction-turn-usage",job_id,rid)


def turn_usage_request_id(job_id,rid):
    return control_key("instruction-turn-usage-request",job_id,rid)


class TurnUsagePrice(_Model):
    """The rates applied to one usage group when it was recorded; None when the deployment had no rate."""
    model:ModelDeployment
    input_microusd_per_million:Money|None
    output_microusd_per_million:Money|None
    metered_microusd:Money|None


class TurnUsageRecord(_Model):
    kind:Literal["instruction_turn_usage"]="instruction_turn_usage"
    job_id:Identifier
    rid:Identifier
    usage:TurnModelUsage
    prices:tuple[TurnUsagePrice,...]
    metered_microusd:Money
    # Sent requests without a known amount: unmeasured, or measured on a deployment without a rate.
    unmetered_requests:Annotated[int,Field(ge=0)]


def turn_usage_cost(usage,prices):
    """Metered money and unmetered request count; never an inferred amount for an unknown request."""
    if len(prices)!=len(usage.usage):
        raise ValueError("every usage group has its applied rates")
    metered,unmetered=0,0
    for group,applied in zip(usage.usage,prices):
        priced=(group.measured_requests>0 and applied.input_microusd_per_million is not None
                and applied.output_microusd_per_million is not None)
        expected=(price(group.input_tokens,group.output_tokens,applied.input_microusd_per_million,
                        applied.output_microusd_per_million) if priced else None)
        if applied.model!=group.model or applied.metered_microusd!=expected:
            raise ValueError("applied rates differ from the reported usage")
        if priced:
            metered+=expected
            unmetered+=group.requests-group.measured_requests
        else:
            unmetered+=group.requests
    return metered,unmetered


def _registered_instruction(control,job_id):
    record=next((r for r in control.records.values() if r.binding.job_id==job_id),None)
    if record is None or record.profile.instruction is None:
        raise Problem(403,"INSTRUCTION_USAGE_SCOPE","turn usage belongs to an original registered instruction work")
    return record


def _ledger_backoff(attempt):
    """A short jittered pause after a lost CAS race on the shared cost row, so racing writers spread out."""
    time.sleep(random.uniform(0.02,0.1)*2**attempt)


def record_turn_usage(control,*,job_id,rid,usage):
    """Write one turn's reported model usage once, before its completion is consumed (ADR-0006 §5 A2).

    A separate CAS keyed by (job_id, rid): the same report replays its original receipt, a different
    report for the same key is refused. On any error the caller does not consume the completion and
    retries later from the same end receipt, so usage is neither lost nor counted twice.
    """
    if not isinstance(usage,TurnModelUsage):
        raise Problem(422,"INSTRUCTION_USAGE_INVALID","a typed turn usage report is required")
    _registered_instruction(control,job_id)
    prices=[]
    for group in usage.usage:
        try:
            model=control.models.select(group.model)
            rates=(model.input_microusd_per_million,model.output_microusd_per_million)
        except Problem as error:
            # Only a deployment absent from the catalog is unpriced; an unreadable catalog raises so the
            # caller does not consume the completion and records this usage later with its rates.
            if error.body["error"]["code"]!="INSTRUCTION_MODEL_UNAVAILABLE":
                raise
            rates=(None,None)
        priced=group.measured_requests>0 and None not in rates
        prices.append(TurnUsagePrice(model=group.model,input_microusd_per_million=rates[0],
            output_microusd_per_million=rates[1],
            metered_microusd=price(group.input_tokens,group.output_tokens,*rates) if priced else None))
    prices=tuple(prices)
    metered,unmetered=turn_usage_cost(usage,prices)
    key=turn_usage_key(job_id,rid)
    request=ControlRequest(request_id=turn_usage_request_id(job_id,rid),
        request_digest=digest([key,job_id,rid,usage.model_dump(mode="json")]),read_set=(control.cost_key,key))
    result={"usage_recorded":True,"metered_microusd":metered,"unmetered_requests":unmetered}

    def decide(rows):
        if rows[key] is not None:
            raise Problem(409,"INSTRUCTION_USAGE_CONFLICT","this turn's usage is already recorded differently")
        ledger=_row(rows,control.cost_key,CostLedger)
        record=TurnUsageRecord(job_id=job_id,rid=rid,usage=usage,prices=prices,
                               metered_microusd=metered,unmetered_requests=unmetered)
        return ControlChanges(writes=(_write(key,record,"create"),_write(control.cost_key,_updated(ledger,
            model_metered_microusd=ledger.model_metered_microusd+metered,
            model_unmetered_requests=ledger.model_unmetered_requests+unmetered),"replace")),result=result)
    for attempt in range(3):
        try:
            # The store replays the original receipt for the same request; another digest is a conflict.
            return control.store.transact(request,decide)
        except Problem as error:
            if error.status!=412 or error.body["error"]["code"]!="BEHAVIOR_CONFLICT" or attempt==2:
                raise
            logging.getLogger(__name__).warning("INSTRUCTION_USAGE_LEDGER_CONFLICT attempt=%s maximum=3 rid=%s",
                                                attempt+1,timing_id(rid))
            _ledger_backoff(attempt)


def replay_turn_usage(view,receipt,ledger,records,ledgerkey):
    """Startup check of one recorded turn usage and the exact ledger change it made."""
    from .behavior_runtime_rotation_history import require
    keys=[k for k,_ in receipt.writes if k in view.inventory.rows
          and view.inventory.rows[k].payload["data"].get("kind")=="instruction_turn_usage"]
    require(len(keys)==1,"unknown turn usage writer")
    key=keys[0]
    final=view.row(key,TurnUsageRecord)
    record=next((r for r in records.values() if r.binding.job_id==final.job_id),None)
    require(record is not None and record.profile.instruction is not None
        and key==turn_usage_key(final.job_id,final.rid),"turn usage escaped its original instruction work")
    try:
        metered,unmetered=turn_usage_cost(final.usage,final.prices)
    except ValueError:
        require(False,"turn usage amount differs from its applied rates")
    require(final.metered_microusd==metered and final.unmetered_requests==unmetered,
            "turn usage totals differ from its groups")
    expected=ControlRequest(request_id=turn_usage_request_id(final.job_id,final.rid),
        request_digest=digest([key,final.job_id,final.rid,final.usage.model_dump(mode="json")]),
        read_set=(ledgerkey,key))
    view.receipt(expected,(key,ledgerkey),exact={key:view.hash(key)},result={
        "usage_recorded":True,"metered_microusd":metered,"unmetered_requests":unmetered})
    return _updated(ledger,model_metered_microusd=ledger.model_metered_microusd+metered,
                    model_unmetered_requests=ledger.model_unmetered_requests+unmetered)


# ADR-0007 native turns: no Table job or WorkRegistration exists, so usage is keyed by the server-resolved
# Agent and the original request ID. "memory" (#315 private-memory service usage) will be a second source
# with its own usage type; it is not accepted yet.
AgentUsageSource = Literal["model"]


def agent_turn_usage_key(agent_oid,rid,source="model"):
    return control_key("agent-turn-usage",agent_oid,rid,source)


def agent_turn_usage_request_id(agent_oid,rid,source="model"):
    return control_key("agent-turn-usage-request",agent_oid,rid,source)


class AgentTurnUsageRecord(_Model):
    kind:Literal["agent_turn_usage"]="agent_turn_usage"
    agent_oid:Identifier
    rid:Identifier
    source:AgentUsageSource="model"
    # None: the turn reached its final state without a trusted usage report; recorded as unknown, never 0.
    usage:TurnModelUsage|None
    prices:tuple[TurnUsagePrice,...]
    metered_microusd:Money
    unmetered_requests:Annotated[int,Field(ge=0)]


def _agent_usage_cost(usage,prices):
    if usage is None:
        if prices:
            raise ValueError("an unknown turn has no applied rates")
        return 0,0
    return turn_usage_cost(usage,prices)


def _agent_usage_request(key,agent_oid,rid,source,usage,ledgerkey):
    return ControlRequest(request_id=agent_turn_usage_request_id(agent_oid,rid,source),
        request_digest=digest([key,agent_oid,rid,source,None if usage is None else usage.model_dump(mode="json")]),
        read_set=(ledgerkey,key))


def _agent_usage_result(usage,metered,unmetered):
    return {"usage_recorded":True,"usage_known":usage is not None,"metered_microusd":metered,
            "unmetered_requests":unmetered}


def agent_usage_scope(control,principal):
    """Only the verified Hosted Agent of this policy reports its own turns; the body never names an Agent."""
    from .behavior_auth import BehaviorPrincipal
    template=control.policy.template
    if (not isinstance(principal,BehaviorPrincipal) or principal.kind!="agent"
            or principal.oid!=template.agent_oid or not principal.actor_id):
        raise Problem(403,"AGENT_USAGE_SCOPE","only the verified Agent of this policy may report its turn usage")
    principal.require(template.team_id,"execute")


def record_agent_turn_usage(control,*,rid,usage):
    """Write one native turn's final usage once into the original CostLedger (ADR-0007).

    ``control`` is the API's own work policy for the Agent whose original receipt this is; the Agent is
    never taken from a request body. Call it only after the turn is final: the same report for the same
    (Agent, RID) replays its original receipt, and a different report is refused with 409. A catalog read
    failure raises before any write. Any other failure (for example a lost commit acknowledgement) may
    leave the result unknown: the write may already exist, so reconcile only by calling again with the
    same report, never with another report or RID.
    """
    if usage is not None and not isinstance(usage,TurnModelUsage):
        raise Problem(422,"AGENT_USAGE_INVALID","a typed turn usage report or an explicit unknown is required")
    agent_oid=control.policy.template.agent_oid
    source="model"
    prices=[]
    for group in () if usage is None else usage.usage:
        try:
            model=control.models.select(group.model)
            rates=(model.input_microusd_per_million,model.output_microusd_per_million)
        except Problem as error:
            # As for job turns: only a deployment absent from the catalog is unpriced; an unreadable
            # catalog raises before any write.
            if error.body["error"]["code"]!="INSTRUCTION_MODEL_UNAVAILABLE":
                raise
            rates=(None,None)
        priced=group.measured_requests>0 and None not in rates
        prices.append(TurnUsagePrice(model=group.model,input_microusd_per_million=rates[0],
            output_microusd_per_million=rates[1],
            metered_microusd=price(group.input_tokens,group.output_tokens,*rates) if priced else None))
    prices=tuple(prices)
    metered,unmetered=_agent_usage_cost(usage,prices)
    key=agent_turn_usage_key(agent_oid,rid,source)
    request=_agent_usage_request(key,agent_oid,rid,source,usage,control.cost_key)
    result=_agent_usage_result(usage,metered,unmetered)

    def decide(rows):
        if rows[key] is not None:
            raise Problem(409,"AGENT_USAGE_CONFLICT","this turn's usage is already recorded differently")
        ledger=_row(rows,control.cost_key,CostLedger)
        record=AgentTurnUsageRecord(agent_oid=agent_oid,rid=rid,source=source,usage=usage,prices=prices,
                                    metered_microusd=metered,unmetered_requests=unmetered)
        return ControlChanges(writes=(_write(key,record,"create"),_write(control.cost_key,_updated(ledger,
            model_metered_microusd=ledger.model_metered_microusd+metered,
            model_unmetered_requests=ledger.model_unmetered_requests+unmetered,
            model_unknown_turns=ledger.model_unknown_turns+(usage is None)),"replace")),result=result)
    for attempt in range(3):
        try:
            # The store replays the original receipt for the same request; another digest is a conflict.
            return control.store.transact(request,decide)
        except Problem as error:
            if error.status!=412 or error.body["error"]["code"]!="BEHAVIOR_CONFLICT" or attempt==2:
                raise
            logging.getLogger(__name__).warning("AGENT_USAGE_LEDGER_CONFLICT attempt=%s maximum=3 rid=%s",
                                                attempt+1,timing_id(rid))
            _ledger_backoff(attempt)


def replay_agent_turn_usage(view,receipt,ledger,agent_oid,ledgerkey):
    """Startup check of one recorded native turn usage and the exact ledger change it made."""
    from .behavior_runtime_rotation_history import require
    keys=[k for k,_ in receipt.writes if k in view.inventory.rows
          and view.inventory.rows[k].payload["data"].get("kind")=="agent_turn_usage"]
    require(len(keys)==1,"unknown agent turn usage writer")
    key=keys[0]
    final=view.row(key,AgentTurnUsageRecord)
    require(final.agent_oid==agent_oid and key==agent_turn_usage_key(final.agent_oid,final.rid,final.source),
            "agent turn usage escaped its original Agent")
    try:
        metered,unmetered=_agent_usage_cost(final.usage,final.prices)
    except ValueError:
        require(False,"agent turn usage amount differs from its applied rates")
    require(final.metered_microusd==metered and final.unmetered_requests==unmetered,
            "agent turn usage totals differ from its groups")
    view.receipt(_agent_usage_request(key,final.agent_oid,final.rid,final.source,final.usage,ledgerkey),
        (key,ledgerkey),exact={key:view.hash(key)},result=_agent_usage_result(final.usage,metered,unmetered))
    return _updated(ledger,model_metered_microusd=ledger.model_metered_microusd+metered,
                    model_unmetered_requests=ledger.model_unmetered_requests+unmetered,
                    model_unknown_turns=ledger.model_unknown_turns+(final.usage is None))

class ModelCost(_Model):
    kind:Literal["instruction_model_cost"]="instruction_model_cost"
    session_id:Identifier
    job_id:Identifier
    request_sha256:Annotated[str,Field(pattern=r"^[a-f0-9]{64}$")]
    model:ModelDeployment
    input_microusd_per_million:Money
    output_microusd_per_million:Money
    request_token_estimate:Annotated[int,Field(ge=0)]
    input_token_upper_bound:Annotated[int,Field(ge=0)]
    output_token_upper_bound:Annotated[int,Field(gt=0)]
    pending_microusd:Money
    stage:Literal["pending","usage"]="pending"
    input_tokens:Annotated[int,Field(ge=0)]|None=None
    output_tokens:Annotated[int,Field(ge=0)]|None=None
    reasoning_tokens:Annotated[int,Field(strict=True,ge=0)]|None=Field(default=None,exclude_if=lambda value:value is None)
    metered_microusd:Money=0


def quote(authority,session,body):
    control=authority.runtime.work_policy
    if session.vm_turn_ticket_id is None:
        control.observe_cost(control.cost_reader(),instruction=True)
    model=control.models.select(session.assignment.task.instruction.model)
    if (model.input_microusd_per_million is None or model.output_microusd_per_million is None
            or model.max_output_tokens is None):
        raise Problem(503,"INSTRUCTION_PRICE_UNAVAILABLE","actual deployment rate/output facts are needed for the shared budget")
    raw=json.dumps(body,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
    tokens=authority.runtime.dep.memory._count(raw)
    if type(tokens) is not int or tokens<0:
        raise Problem(503,"INSTRUCTION_TOKEN_ESTIMATE_UNAVAILABLE","local request token estimate is unavailable")
    # UTF-8 bytes conservatively bound the token count of the configured OpenAI
    # request, including message/tool framing; this is not a provider context cap.
    upper=max(tokens,len(raw))
    pending=price(upper,model.max_output_tokens,model.input_microusd_per_million,model.output_microusd_per_million)
    return ModelCost(session_id=session.assignment.session_id,job_id=session.execution.job_id,
        request_sha256=digest(body),model=model.id,input_microusd_per_million=model.input_microusd_per_million,
        output_microusd_per_million=model.output_microusd_per_million,request_token_estimate=tokens,
        input_token_upper_bound=upper,output_token_upper_bound=model.max_output_tokens,pending_microusd=pending)


def price(input_tokens,output_tokens,input_rate,output_rate):
    return (input_tokens*input_rate+output_tokens*output_rate+999999)//1000000


def reserve_writes(control,rows,value,*,admitted_policy=None):
    ledger=_row(rows,control.cost_key,CostLedger)
    if admitted_policy is None:
        policy=control.installed(rows)
    else:
        record=_registered_instruction(control,value.job_id)
        if (record.agent_vm is None or admitted_policy!=control.policy
                or record.policy_sha256!=admitted_policy.sha256):
            raise Problem(403,"INSTRUCTION_USAGE_SCOPE","the original admitted VM policy is required")
        policy=admitted_policy
    total=(ledger.observed_high_water_microusd+ledger.legacy_unresolved_reserve_microusd+
        ledger.work_reservations_microusd+ledger.model_pending_microusd+ledger.model_metered_microusd+
        value.pending_microusd+policy.safety_reserve_microusd)
    if not policy.active or ledger.budget_enforced and total>policy.cumulative_ceiling_microusd:
        raise Problem(403,"WORK_POLICY_CUMULATIVE_BUDGET_EXHAUSTED","original shared USD budget cannot cover this pending request")
    key=cost_key(value.session_id,value.request_sha256)
    if rows[key] is not None:
        raise Problem(409,"ASSISTANT_MODEL_UNKNOWN","original paid request is already retained; do not send again")
    return (_write(key,value,"create"),_write(control.cost_key,_updated(ledger,
        model_pending_microusd=ledger.model_pending_microusd+value.pending_microusd),"replace"))


def usage_facts(raw, *, wire_api="completions"):
    """Observability only: provider token counts, cached prompt tokens and tool calls in one model stream.

    Never raises and never reads model text; missing or malformed facts are reported as None.
    """
    from .behavior_instruction_stream import SseDataEvents
    facts={"prompt_tokens":None,"completion_tokens":None,"cached_tokens":None,"tool_calls":0}
    if wire_api=="responses":
        facts["reasoning_tokens"]=None
    calls=set()
    try:
        for data in SseDataEvents().feed(raw):
            if data.strip()==b"[DONE]":continue
            try:value=json.loads(data)
            except (ValueError,UnicodeError):continue
            if type(value) is not dict:continue
            if wire_api=="responses":
                item=value.get("item")
                if value.get("type")=="response.output_item.done" and isinstance(item,dict) and item.get("type")=="function_call":
                    calls.add(item.get("call_id"))
                if value.get("type") not in {"response.completed","response.incomplete"}:continue
                usage=(value.get("response") or {}).get("usage")
                if not isinstance(usage,dict):continue
                for target,source in (("prompt_tokens","input_tokens"),("completion_tokens","output_tokens")):
                    if type(usage.get(source)) is int:facts[target]=usage[source]
                for target,source,field in (("cached_tokens","input_tokens_details","cached_tokens"),
                                            ("reasoning_tokens","output_tokens_details","reasoning_tokens")):
                    details=usage.get(source)
                    if isinstance(details,dict) and type(details.get(field)) is int:facts[target]=details[field]
                continue
            for choice in value.get("choices") or ():
                delta=choice.get("delta") if type(choice) is dict else None
                for call in (delta or {}).get("tool_calls") or () if type(delta) is dict else ():
                    if type(call) is dict:
                        calls.add((choice.get("index"),call.get("index")))
            usage=value.get("usage")
            if type(usage) is not dict:continue
            for name in ("prompt_tokens","completion_tokens"):
                if type(usage.get(name)) is int:facts[name]=usage[name]
            details=usage.get("prompt_tokens_details")
            if type(details) is dict and type(details.get("cached_tokens")) is int:
                facts["cached_tokens"]=details["cached_tokens"]
    except Exception:
        pass
    facts["tool_calls"]=len(calls)
    return facts


def usage_from_sse(raw, *, wire_api="completions"):
    """Read only standard usage facts; never persist model text or reasoning."""
    from .behavior_instruction_stream import SseDataEvents
    if wire_api not in {"completions","responses"}:
        raise Problem(503,"INSTRUCTION_USAGE_UNKNOWN","the original model wire API is unsupported")
    found=None
    for data in SseDataEvents().feed(raw):
        if data.strip()==b"[DONE]":continue
        try:value=json.loads(data)
        except (ValueError,UnicodeError):continue
        if wire_api=="responses":
            if not isinstance(value,dict) or value.get("type") not in {"response.completed","response.incomplete"}:
                continue
            response=value.get("response")
            usage=response.get("usage") if isinstance(response,dict) else None
        else:
            usage=value.get("usage") if type(value) is dict else None
        if usage is None:continue
        fields=("input_tokens","output_tokens") if wire_api=="responses" else ("prompt_tokens","completion_tokens")
        counts=tuple(usage.get(k) for k in fields) if type(usage) is dict else ()
        if len(counts)!=2 or any(type(n) is not int or n<0 for n in counts):
            raise Problem(503,"INSTRUCTION_USAGE_UNKNOWN","provider usage is invalid; original pending cost remains")
        if wire_api=="responses":
            details=usage.get("output_tokens_details")
            if details is not None:
                if not isinstance(details,dict):
                    raise Problem(503,"INSTRUCTION_USAGE_UNKNOWN","provider reasoning usage is invalid")
                reasoning=details.get("reasoning_tokens")
                if reasoning is not None:
                    if type(reasoning) is not int or not 0<=reasoning<=counts[1]:
                        raise Problem(503,"INSTRUCTION_USAGE_UNKNOWN","provider reasoning usage is invalid")
                    counts=(*counts,reasoning)
        if found is not None and found!=counts:
            raise Problem(503,"INSTRUCTION_USAGE_UNKNOWN","provider usage conflicts; original pending cost remains")
        found=counts
    return found


def record_usage(authority,session,request_sha256,counts):
    if len(counts) not in {2,3} or any(type(n) is not int or n<0 for n in counts) or len(counts)==3 and counts[2]>counts[1]:
        raise Problem(503,"INSTRUCTION_USAGE_UNKNOWN","provider usage is invalid; original pending cost remains")
    control=authority.runtime.work_policy
    key=cost_key(session.assignment.session_id,request_sha256)
    request=ControlRequest(request_id=control_key("instruction-model-usage",key),
        request_digest=digest([key,*counts]),read_set=(control.cost_key,key))
    def decide(rows):
        original=_row(rows,key,ModelCost)
        ledger=_row(rows,control.cost_key,CostLedger)
        if (original.session_id!=session.assignment.session_id or original.job_id!=session.execution.job_id
                or original.request_sha256!=request_sha256 or original.stage!="pending"
                or ledger.model_pending_microusd<original.pending_microusd):
            raise Problem(409,"INSTRUCTION_USAGE_UNKNOWN","original pending usage association differs")
        measured=price(counts[0],counts[1],original.input_microusd_per_million,original.output_microusd_per_million)
        updated=_updated(original,stage="usage",input_tokens=counts[0],output_tokens=counts[1],
                         reasoning_tokens=counts[2] if len(counts)==3 else None,metered_microusd=measured)
        return ControlChanges(writes=(_write(key,updated,"replace"),_write(control.cost_key,
            _updated(ledger,model_pending_microusd=ledger.model_pending_microusd-original.pending_microusd,
                     model_metered_microusd=ledger.model_metered_microusd+measured),"replace")),
            result={"request_sha256":request_sha256,"usage_recorded":True,"retail_estimate_microusd":measured})
    from .behavior_assistant import _ledger_conflicts
    return (_ledger_conflicts(authority.store,request,decide) if authority.runtime.mode=="native"
            else authority.store.transact(request,decide))


def replay_cost_write(view,receipt,ledger,records,policy):
    from .behavior_assistant import _Session,_ModelIntent,key as assistant_key
    from .behavior_runtime_rotation_history import require
    keys=[k for k,_ in receipt.writes if k in view.inventory.rows
          and view.inventory.rows[k].payload["data"].get("kind")=="instruction_model_cost"]
    require(len(keys)==1,"unknown instruction cost writer")
    key=keys[0]
    final=view.row(key,ModelCost)
    record=next((r for r in records.values() if r.binding.job_id==final.job_id),None)
    require(record is not None and record.profile.instruction is not None
        and final.model==record.profile.instruction.model
        and key==cost_key(final.session_id,final.request_sha256),"instruction cost escaped its original work/model")
    session=view.row(assistant_key("session",final.session_id),_Session)
    intentkey=assistant_key("model-intent",final.session_id,final.request_sha256)
    intent=view.row(intentkey,_ModelIntent)
    require(session.execution.job_id==final.job_id and intent.consumed
        and final.pending_microusd==price(final.input_token_upper_bound,final.output_token_upper_bound,
            final.input_microusd_per_million,final.output_microusd_per_million),
        "pending model estimate or original send intent differs")
    from .behavior_work_policy import work_policy_key
    ledgerkey=work_policy_key("cost",policy.authority_domain)
    if receipt.request.request_id==assistant_key("model-send",final.session_id,final.request_sha256):
        pending=_updated(final,stage="pending",input_tokens=None,output_tokens=None,reasoning_tokens=None,metered_microusd=0)
        require({key,ledgerkey,intentkey}<=set(receipt.request.read_set),"model budget CAS predicate is missing")
        view.receipt(receipt.request,(key,ledgerkey,intentkey),exact={
            key:view.image_hash(key,pending),intentkey:view.image_hash(intentkey,intent)},result={"state":"accepted"})
        total=(ledger.observed_high_water_microusd+ledger.legacy_unresolved_reserve_microusd+
            ledger.work_reservations_microusd+ledger.model_pending_microusd+ledger.model_metered_microusd+
            final.pending_microusd+policy.safety_reserve_microusd)
        require(not ledger.budget_enforced or total<=policy.cumulative_ceiling_microusd,
                "model dispatch exceeded the shared original budget")
        return _updated(ledger,model_pending_microusd=ledger.model_pending_microusd+final.pending_microusd)
    require(final.stage=="usage" and final.input_tokens is not None and final.output_tokens is not None
        and (final.reasoning_tokens is None or final.reasoning_tokens<=final.output_tokens)
        and ledger.model_pending_microusd>=final.pending_microusd
        and final.metered_microusd==price(final.input_tokens,final.output_tokens,
            final.input_microusd_per_million,final.output_microusd_per_million),"model usage estimate differs")
    counts=(final.input_tokens,final.output_tokens)+(() if final.reasoning_tokens is None else (final.reasoning_tokens,))
    expected=ControlRequest(request_id=control_key("instruction-model-usage",key),
        request_digest=digest([key,*counts]),read_set=(ledgerkey,key))
    view.receipt(expected,(key,ledgerkey),exact={key:view.hash(key)},result={
        "request_sha256":final.request_sha256,"usage_recorded":True,"retail_estimate_microusd":final.metered_microusd})
    return _updated(ledger,model_pending_microusd=ledger.model_pending_microusd-final.pending_microusd,
                    model_metered_microusd=ledger.model_metered_microusd+final.metered_microusd)
