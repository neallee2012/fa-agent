"""Pinned standing HTTP adapter. Pure preflight is never dispatch permission."""
from datetime import timedelta
from typing import Literal

from pydantic import model_validator

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import AssistantSubmit
from shared_context.behavior_work_policy import (
    SteadyWorkPolicy, WorkPreflight, WorkPreflightQuery, WorkPolicyDecision, derive_work, work_policy_key,
)
from shared_context.memory_bridge_settings import with_memory_slots
from .adapter import ConsoleError, PullAdapter, SteadyConsoleConfig, require, sha, strict_json
from .work_policy import CumulativeBudget, WorkPolicyObservation


def submit_contract_digest(task):
    return sha(AssistantSubmit(request_id=task.submit_request_id).model_dump_json().encode("utf-8"))


def task_matches(task, binding):
    return (task.binding_id == binding.binding_id and task.job_id == binding.job_id
            and task.work_id == binding.work.work_id and task.event_id == binding.event_id
            and task.root_request_id == binding.root_request_id and task.obligation_id == binding.obligation_id
            and task.owner_record_id == binding.owner.owner_record_id
            and task.route.model_dump(mode="json") == binding.route.model_dump(mode="json"))


class StandingConsoleConfig(SteadyConsoleConfig):
    schema_version: Literal["fa-web-test-console-standing-v3"] = "fa-web-test-console-standing-v3"
    standing_policy: SteadyWorkPolicy
    standing_policy_sha256: wire.Digest

    @model_validator(mode="after")
    def pinned_derivation(self):
        p = self.standing_policy
        if (p.sha256 != self.standing_policy_sha256 or p.configuration_sha256 != self.backend_configuration_sha256
                or p.template.tenant_id != self.tenant_id or p.template.human_oid != self.human_oid
                or p.template.route.recipient_id != self.human_oid):
            raise ValueError("exact trusted standing policy/configuration/identity required")
        for task in self.tasks:
            if task.mode == "normal_pull":
                _, expected, _ = derive_work(p, task.submit_request_id, p.evidence_mode)
                if task.submission != "assistant" or not task_matches(task, expected):
                    raise ValueError("standing tasks must exactly match the original deterministic policy derivation")
        return self


class StandingSubmitReply(wire.WireModel):
    binding_id: wire.Identifier
    status: wire.JobStatus


class StandingPullAdapter(PullAdapter):
    def request(self, task, action):
        if action == "submit":
            require(task.mode == "normal_pull" and task.submission == "assistant", "ACTION_NOT_ALLOWED", 403)
            return "POST", "/work-policy/v1/submit", AssistantSubmit(request_id=task.submit_request_id).model_dump(mode="json")
        return super().request(task, action)

    def submit(self, task, token):
        raw, _ = self.send(*self.request(task, "submit"), token)
        try:
            data = strict_json(raw)
            require(type(data) is dict and set(data) == {"binding_id", "status"}, "STATUS_INVALID", 502)
            reply = StandingSubmitReply.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("STATUS_INVALID", 502) from None
        require(reply.binding_id == task.binding_id and reply.status.job_id == task.job_id
                and reply.status.work_id == task.work_id, "STATUS_BINDING_MISMATCH", 502)
        return reply.status


class StandingWorkObservation(WorkPolicyObservation):
    preflight: WorkPreflight


class StandingPolicyReader:
    """One fixed pure-read endpoint, only from a trusted complete policy pin."""
    def __init__(self, config, adapter, clock):
        require(type(config) is StandingConsoleConfig and type(adapter) is StandingPullAdapter,
                "WORK_POLICY_INVALID", 502)
        self.config, self.adapter, self.clock = config, adapter, clock
        self.last_guard_revision = None

    def read(self, query, bearer):
        c, p = self.config, self.config.standing_policy
        task = next((t for t in c.tasks if t.mode == "normal_pull" and t.submit_request_id == query.submit_request_id), None)
        require(task is not None and query.backend_origin == c.backend_origin
                and query.backend_configuration_sha256 == c.backend_configuration_sha256
                and query.backend_core_sha256 == c.backend_core_sha256
                and (query.tenant_id, query.human_oid, query.client_id) == (c.tenant_id, c.human_oid, c.client_id)
                and (query.binding_id, query.job_id, query.work_id, query.root_request_id, query.route) ==
                    (task.binding_id, task.job_id, task.work_id, task.root_request_id, task.route)
                and query.submit_body_sha256 == submit_contract_digest(task), "WORK_POLICY_INVALID", 502)
        body = WorkPreflightQuery(read_id=query.read_id, request_id=task.submit_request_id,
                                  submit_body_sha256=submit_contract_digest(task))
        raw, _ = self.adapter.send("POST", "/work-policy/v1/preflight", body.model_dump(mode="json"),
                                  bearer.get_secret_value())
        _, binding, _ = derive_work(p, task.submit_request_id, p.evidence_mode)
        return self.checked(query, task, binding, raw)

    def checked(self, query, task, binding, raw, *, instruction=False, previous_guard=None):
        c, p = self.config, self.config.standing_policy
        result = _parsed_preflight(raw)
        if result.memory_setting_ref is not None:
            ref = result.memory_setting_ref
            admission = work_policy_key("admission", *p.scope_ids, p.template.human_oid, task.submit_request_id)
            require(instruction and ref.admission_id == admission
                    and (not result.already_admitted or ref.state == "captured"), "WORK_POLICY_INVALID", 502)
            binding = with_memory_slots(binding, admission)
        money = result.monetary_budget
        _check_clock(result, self.clock(), instruction)
        _check_deadlines(result, p)
        _check_echo(result, query, c, task, binding)
        _check_budget_owner(result, p)
        _check_budget_bounds(result, p)
        _check_spend(result, p, instruction, previous_guard if instruction else self.last_guard_revision)
        _check_admission(result, instruction)
        _check_allow(result)
        if not instruction:
            self.last_guard_revision = result.guard_revision
        return StandingWorkObservation(query=query, decision=result.decision, reason=_observation_reason(result),
            revision=str(result.guard_revision), observed_at=result.observed_at,
            execution_deadline=result.execution_deadline,
            budget=CumulativeBudget(budget_id=money.budget_id, unit="USD-micro",
                                    remaining=money.remaining_microusd, required=money.required_microusd,
                                    enforced=money.budget_enforced),
            preflight=result)


def _parsed_preflight(raw):
    """The preflight, rejected unless its JSON keys are exactly the model's own (no defaults filled in)."""
    try:
        data = strict_json(raw)
        result = WorkPreflight.model_validate_json(raw)
        require(type(data) is dict and set(data) == set(result.model_dump(mode="json"))
                and type(data["monetary_budget"]) is dict
                and set(data["monetary_budget"]) == set(result.monetary_budget.model_dump(mode="json")),
                "WORK_POLICY_INVALID", 502)
    except (ValueError, TypeError, KeyError):
        raise ConsoleError("WORK_POLICY_INVALID", 502) from None
    return result


def _check_clock(result, now, instruction):
    """Every time is UTC and none is ahead of the read; an instruction read carries no deadlines."""
    money = result.monetary_budget
    times = (result.observed_at, money.observed_at, result.execution_deadline, result.queue_deadline)
    if instruction:
        require(result.execution_deadline is None and result.queue_deadline is None, "WORK_POLICY_INVALID", 502)
    require(all(t is None or t.tzinfo is not None and t.utcoffset() == timedelta(0) for t in times)
            and money.observed_at <= result.observed_at
            and (instruction or result.observed_at <= now), "WORK_POLICY_INVALID", 502)


def _check_deadlines(result, p):
    # Runs after _check_clock: deadlines are not UTC-validated on parse, and a naive one cannot be compared.
    require((result.queue_deadline is None or result.queue_deadline <= result.observed_at+timedelta(seconds=p.queue_seconds))
            and (result.execution_deadline is None or result.queue_deadline is not None
                 and result.execution_deadline <= result.observed_at+timedelta(seconds=p.execution_seconds)
                 and result.execution_deadline <= result.queue_deadline+timedelta(seconds=p.execution_seconds)),
            "WORK_POLICY_INVALID", 502)


def _check_echo(result, query, c, task, binding):
    """The read answers this query under the pinned configuration, policy, identity and binding."""
    p, money = c.standing_policy, result.monetary_budget
    require(result.read_id == money.read_id == query.read_id
            and result.request_id == query.submit_request_id
            and result.submit_body_sha256 == query.submit_body_sha256
            and result.configuration_sha256 == c.backend_configuration_sha256
            and result.policy_sha256 == c.standing_policy_sha256
            and (result.tenant_id, result.team_id, result.human_oid, result.template_binding_id) ==
                (p.template.tenant_id, p.template.team_id, p.template.human_oid, p.template.binding_id)
            and result.binding == binding and task_matches(task, result.binding), "WORK_POLICY_INVALID", 502)


def _check_budget_owner(result, p):
    """The money decision is this policy's cost budget, and nothing in the read authorizes anything."""
    money = result.monetary_budget
    require((money.policy_id, money.policy_revision, money.budget_id) ==
            (p.policy_id, p.revision, work_policy_key("cost", p.authority_domain))
            and money.unit == "USD-micro" and not money.authorization
            and not money.provider_hard_cap and not result.authorization,
            "WORK_POLICY_INVALID", 502)


def _check_budget_bounds(result, p):
    """An enforced budget reports the policy's ceiling and reserve and their exact remainder; otherwise none."""
    money = result.monetary_budget
    # The int check runs first: it keeps None out of the remainder arithmetic.
    bounded = (money.remaining_microusd, money.safety_reserve_microusd, money.cumulative_ceiling_microusd)
    require((all(type(value) is int and 0 <= value <= 2**63-1 for value in bounded)
             and money.cumulative_ceiling_microusd == p.cumulative_ceiling_microusd
             and money.safety_reserve_microusd == p.safety_reserve_microusd
             and money.remaining_microusd == max(0, money.cumulative_ceiling_microusd
                - money.safety_reserve_microusd - money.observed_high_water_microusd - money.reserved_microusd))
            if money.budget_enforced else all(value is None for value in bounded),
            "WORK_POLICY_INVALID", 502)


def _check_spend(result, p, instruction, prior_guard):
    """This read's own amounts, its required cost, and a guard revision that never goes back."""
    money = result.monetary_budget
    amounts = (money.required_microusd, money.observed_high_water_microusd, money.reserved_microusd)
    require(all(type(value) is int and 0 <= value <= 2**63-1 for value in amounts)
            and money.observed_high_water_microusd >= p.minimum_observed_microusd
            and money.required_microusd == (0 if instruction or result.already_admitted else p.work_estimate_microusd)
            and type(result.guard_revision) is int and result.guard_revision >= 0
            and (prior_guard is None or result.guard_revision >= prior_guard),
            "WORK_POLICY_INVALID", 502)


def _check_admission(result, instruction):
    """An admitted read is only its original replay; a new one has no execution deadline yet."""
    money = result.monetary_budget
    if result.already_admitted:
        require(money.decision == result.decision == "allow"
                and money.reason == result.reason == "original_admission_replay_only"
                and (instruction or result.queue_deadline is not None), "WORK_POLICY_INVALID", 502)
    else:
        # Registration can persist before its job admission. Preserve its
        # original queue deadline rather than treating it as a new work.
        require(result.execution_deadline is None
                and money.reason != "original_admission_replay_only", "WORK_POLICY_INVALID", 502)


def _check_allow(result):
    """An allow is backed by an allowed money decision with the same reason and enough remaining budget."""
    money = result.monetary_budget
    require(result.decision != "allow" or (
        money.decision == "allow" and result.reason == money.reason
        and money.reason in {"allowed", "original_admission_replay_only"}
        and (not money.budget_enforced or money.remaining_microusd >= money.required_microusd)),
        "WORK_POLICY_INVALID", 502)


def _observation_reason(result):
    if result.decision == "allow":
        return "within_budget" if result.monetary_budget.budget_enforced else "accounting_only"
    if result.reason in {"cumulative_budget_exhausted", "abstract_root_budget_exhausted"}:
        return "budget_exhausted"
    return "revoked" if "revoked" in result.reason else "not_ready"
