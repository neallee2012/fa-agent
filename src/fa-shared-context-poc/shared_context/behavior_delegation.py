"""Server-internal receipts for one SDK-owned synchronous, zero-tool child.

This module never spawns a child or grants capability access. A trusted admission
caller must authorize/capture the capability before using it; that wiring is
separate. A fresh reservation is not reusable dispatch authority, and historical
receipts never permit another spawn. The SDK retains its agent/tool loop.
"""

from dataclasses import dataclass
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, ValidationError, model_validator

from .behavior_operations import (
    BudgetRecord, ExecutionContext, OwnerRecord, PolicyRecord, _Model, _checked,
    _row, _updated, _write, control_key,
)
from .behavior_runtime import material
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, Digest
from .behavior_workers import ExecutorBinding, JobRecord, worker_key
from .errors import Problem
from .models import Identifier
from .source_materials import digest
from .behavior_delegation_wire import (
    CAPABILITY_ID, CHILD_AGENT_TYPE, DelegationPublicText, DelegationStatus, DelegationTask,
    pack_delegation_text, unpack_delegation_text,
)

if TYPE_CHECKING:
    from .behavior_assistant import AssistantAuthority, _Session


class DelegationParent(_Model):
    tenant_id: Identifier
    team_id: Identifier
    binding_id: Identifier
    parent_request_id: Identifier
    root_request_id: Identifier
    registration_id: Identifier
    registration_sha256: Digest
    session_id: Identifier
    sdk_session_id: Identifier
    profile_sha256: Digest
    capability_id: Literal["fa-delegate-v1"] = "fa-delegate-v1"
    capability_sha256: Digest
    execution: ExecutorBinding


class _Observation(_Model):
    event_id: Identifier
    tool_call_id: Identifier
    child_agent_id: Identifier | None = None


class _ResultObservation(_Observation):
    child_agent_id: Identifier
    outcome: Literal["completed", "failed", "cancelled"]
    text: str | None
    ended_in_tool_call: bool = False
    failure_code: Identifier | None = None

    @model_validator(mode="after")
    def failure(self):
        if (self.outcome == "failed") != (self.failure_code is not None):
            raise ValueError("only an explicit failed child supplies its public failure code")
        return self


class _UnknownObservation(_Observation):
    reason_code: Identifier


def _observation(model, **values):
    try:
        return model.model_validate(values)
    except ValidationError:
        raise Problem(422, "DELEGATION_OBSERVATION_INVALID", "an exact original public observation is required") from None


class DelegationIntent(_Model):
    kind: Literal["sdk_delegation_intent"] = "sdk_delegation_intent"
    delegation_id: Identifier
    parent: DelegationParent
    tool_call_id: Identifier
    input_sha256: Digest
    request: ControlRequest

    @property
    def root_budget_id(self):
        return self.parent.execution.owner.root_budget_id


class DelegationState(_Model):
    kind: Literal["sdk_delegation_state"] = "sdk_delegation_state"
    delegation_id: Identifier
    status: DelegationStatus = "unknown"
    child_agent_id: Identifier | None = None
    result_id: Identifier | None = None
    failure_code: Identifier | None = None
    cancel_request_id: Identifier | None = None
    child_cancelled_event_id: Identifier | None = None
    parent_idle_event_id: Identifier | None = None
    request: ControlRequest

    @model_validator(mode="after")
    def outcome(self):
        stopped = self.child_cancelled_event_id is not None and self.parent_idle_event_id is not None
        if stopped != (self.status == "cancelled"):
            raise ValueError("cancelled requires both original child and parent stop observations")
        if (self.status in {"running", "completed", "failed", "cancelled", "incomplete"}
                or self.result_id is not None or self.child_cancelled_event_id is not None
                or self.parent_idle_event_id is not None) and self.child_agent_id is None:
            raise ValueError("child outcome requires the original child identity")
        if self.status == "completed" and self.result_id is None:
            raise ValueError("completed requires a saved full result")
        if self.status == "failed" and self.failure_code is None:
            raise ValueError("failed requires an explicit public failure")
        if self.status == "incomplete" and self.failure_code != "DELEGATION_RESULT_INCOMPLETE":
            raise ValueError("incomplete cannot become a completed result")
        return self


class DelegationResult(_Model):
    kind: Literal["sdk_delegation_result"] = "sdk_delegation_result"
    delegation_id: Identifier
    child_agent_id: Identifier
    data: Annotated[bytes, Field(max_length=32768)]
    sha256: Digest
    byte_length: Annotated[int, Field(ge=0, le=4 * 1024 * 1024)]
    request: ControlRequest


class DelegationModelUsage(_Model):
    kind: Literal["sdk_delegation_model_usage"] = "sdk_delegation_model_usage"
    delegation_id: Identifier
    child_agent_id: Identifier
    parent_work_id: Identifier
    root_request_id: Identifier
    root_budget_id: Identifier
    session_id: Identifier
    job_id: Identifier
    request_sha256: Digest
    model_cost_id: Identifier
    request: ControlRequest


class DelegationView(_Model):
    delegation_id: Identifier
    parent: DelegationParent
    tool_call_id: Identifier
    input_sha256: Digest
    status: DelegationStatus
    child_agent_id: Identifier | None
    result: str | None = None
    failure_code: Identifier | None = None
    cancel_requested: bool = False
    child_cancelled: bool = False
    parent_idle: bool = False
    stop_confirmed: bool = False


@dataclass(frozen=True)
class DelegationReservation:
    fresh: bool
    view: DelegationView


def delegation_key(work_id):
    return control_key("sdk-delegation", work_id)


def state_key(delegation_id):
    return control_key("sdk-delegation-state", delegation_id)


def result_key(delegation_id):
    return control_key("sdk-delegation-result", delegation_id)


def usage_key(model_cost_id):
    return control_key("sdk-delegation-model-usage", model_cost_id)


def capture_parent(authority: "AssistantAuthority", session: "_Session", *, capability_sha256: str):
    """Capture server-owned work identity, not a human capability approval."""
    from .behavior_work_policy import work_policy_key
    control = authority.runtime.work_policy
    if control is None:
        raise Problem(403, "DELEGATION_PARENT_REQUIRED", "an original registered instruction work is required")
    binding = authority._binding(session)
    record = control.records.get(binding.binding_id)
    if record is None or record.profile.instruction is None or record.binding != binding:
        raise Problem(403, "DELEGATION_PARENT_REQUIRED", "the original work registration is required")
    return DelegationParent(
        tenant_id=binding.tenant_id, team_id=binding.team_id, binding_id=binding.binding_id,
        parent_request_id=record.request_id, root_request_id=binding.root_request_id,
        registration_id=work_policy_key("registration", record.admission_id),
        registration_sha256=digest(record.model_dump(mode="json")),
        session_id=session.assignment.session_id, sdk_session_id=session.assignment.sdk_session_id,
        profile_sha256=session.assignment.task.profile_digest, capability_sha256=capability_sha256,
        execution=session.execution,
    )


def _parent_keys(parent):
    from .behavior_assistant import key
    owner = parent.execution.owner
    return (parent.registration_id, key("session", parent.session_id),
            worker_key("job", parent.execution.job_id), control_key("owner", owner.owner_record_id),
            control_key("policy", owner.policy_id), control_key("budget", owner.root_budget_id))


def _current_parent(rows, parent, *, executing=True):
    from .behavior_assistant import _Session, key
    from .behavior_work_policy import WorkRegistration
    record = _row(rows, parent.registration_id, WorkRegistration)
    session = _row(rows, key("session", parent.session_id), _Session)
    job = _row(rows, worker_key("job", parent.execution.job_id), JobRecord)
    binding, owner = record.binding, parent.execution.owner
    current = _row(rows, control_key("owner", owner.owner_record_id), OwnerRecord)
    policy = _row(rows, control_key("policy", owner.policy_id), PolicyRecord)
    budget = _row(rows, control_key("budget", owner.root_budget_id), BudgetRecord)
    if (record.request_id != parent.parent_request_id or binding.binding_id != parent.binding_id
            or binding.root_request_id != parent.root_request_id
            or (binding.tenant_id, binding.team_id) != (parent.tenant_id, parent.team_id)
            or digest(record.model_dump(mode="json")) != parent.registration_sha256
            or digest(record.profile.model_dump(mode="json")) != parent.profile_sha256
            or record.profile.instruction is None
            or session.assignment.task.instruction != record.profile.instruction
            or session.assignment.task.profile_digest != parent.profile_sha256
            or session.assignment.session_id != parent.session_id
            or session.assignment.sdk_session_id != parent.sdk_session_id
            or session.binding_id != parent.binding_id or session.execution != parent.execution
            or binding.job_id != parent.execution.job_id or binding.work.work_id != parent.execution.work_id
            or binding.owner != owner or job.binding != parent.execution or job.owner != owner
            or any(getattr(current, field) != getattr(owner, field) for field in ExecutionContext.model_fields)):
        raise Problem(403, "DELEGATION_PARENT_CHANGED", "delegation must retain the original parent work and attempt")
    if executing and (not current.active or not policy.active or not budget.active
            or policy.revision != record.runtime_policy.operation_policy_revision
            or job.cancel_requested or job.state not in {"starting", "running", "capacity_uncertain"}
            or session.state != "running"):
        raise Problem(403, "DELEGATION_PARENT_INACTIVE", "only the current active parent may reserve a child")


def _result_text(value):
    return unpack_delegation_text(DelegationPublicText(
        data=value.data, sha256=value.sha256, byte_length=value.byte_length))


def _request(parent, tool_call_id, input_sha256):
    identifier = delegation_key(parent.execution.work_id)
    return ControlRequest(
        request_id=identifier,
        request_digest=digest([parent.model_dump(mode="json"), tool_call_id, input_sha256]),
        read_set=(*_parent_keys(parent), identifier, state_key(identifier)),
    )


def _parent_model_cost(rows, parent, request_sha256):
    from .behavior_assistant import _ModelIntent, key
    from .behavior_instruction_cost import ModelCost, cost_key
    from .behavior_work_policy import WorkRegistration
    cost = _row(rows, cost_key(parent.session_id, request_sha256), ModelCost)
    intent = _row(rows, key("model-intent", parent.session_id, request_sha256), _ModelIntent)
    registration = _row(rows, parent.registration_id, WorkRegistration)
    if (cost.session_id != parent.session_id or cost.job_id != parent.execution.job_id
            or cost.request_sha256 != request_sha256 or not intent.consumed
            or intent.request_sha256 != request_sha256 or registration.profile.instruction is None
            or cost.model != registration.profile.instruction.model):
        raise Problem(409, "DELEGATION_USAGE_SCOPE", "child model use must remain on the original parent's model cost")
    return cost


def validate_delegation_history(inventory, config, bindings, *, row_ids=None):
    """Validate original parent/receipt links, not a new capability authorization."""
    from .behavior_assistant import key as assistant_key
    from .behavior_instruction_cost import cost_key
    from .behavior_recovery_history import _Inventory
    from .behavior_work_policy import WorkRegistration
    models = {model.model_fields["kind"].default: model for model in (
        DelegationIntent, DelegationState, DelegationResult, DelegationModelUsage)}
    selected = {key: models[row.payload["data"]["kind"]]
                for key, row in inventory.rows.items()
                if (row_ids is None or key in row_ids) and row.payload["data"].get("kind") in models}
    if not selected:
        return {}
    view = _Inventory(inventory, config)
    values = {key: view.row(key, model, material=True
                           if model in {DelegationIntent, DelegationModelUsage} else None)
              for key, model in selected.items()}
    registered = {binding.binding_id: binding for binding in bindings}
    checked = set()

    def require(condition, message):
        if not condition:
            raise Problem(409, "DELEGATION_HISTORY_INVALID", message)

    def written(key, request, expected_writes):
        return view.receipt(request, expected_writes, exact={key: view.hash(key)})

    for identifier in {value.delegation_id for value in values.values()}:
        intent = view.row(identifier, DelegationIntent, material=True)
        parent = intent.parent
        state_id = state_key(identifier)
        state = view.row(state_id, DelegationState)
        registration = view.row(parent.registration_id, WorkRegistration)
        require(registered.get(parent.binding_id) == registration.binding
                and identifier == intent.delegation_id == delegation_key(parent.execution.work_id)
                and state.delegation_id == identifier
                and intent.request == _request(parent, intent.tool_call_id, intent.input_sha256),
                "delegation escaped its original registered parent")
        _current_parent({key: inventory.rows.get(key) for key in _parent_keys(parent)}, parent, executing=False)
        initial = DelegationState(delegation_id=identifier, request=intent.request)
        view.receipt(intent.request, (identifier, state_id),
                     exact={identifier: view.hash(identifier), state_id: view.image_hash(state_id, initial)},
                     result={"delegation_id": identifier, "status": "unknown"})
        checked.update((identifier, state_id))
        result = None
        if state.result_id is not None:
            require(state.result_id == result_key(identifier), "child result key differs")
            result = view.row(state.result_id, DelegationResult)
            require(result.delegation_id == identifier and result.child_agent_id == state.child_agent_id
                    and result.request.read_set == (*_parent_keys(parent), identifier, state_id, state.result_id),
                    "child result scope or original predicates differ")
            _result_text(result)
            written(state.result_id, result.request, (state_id, state.result_id))
            checked.add(state.result_id)
        if state.request == intent.request:
            writes = (identifier, state_id)
        else:
            require(state.request.read_set in {
                (*_parent_keys(parent), identifier, state_id),
                (*_parent_keys(parent), identifier, state_id, result_key(identifier)),
            }, "child observation predicates escaped the parent")
            writes = ((state_id, state.result_id) if result is not None and state.request == result.request
                      else (state_id,))
        written(state_id, state.request, writes)
        for event_id, result_observation in ((state.child_cancelled_event_id, True),
                                             (state.parent_idle_event_id, False)):
            if event_id is None:
                continue
            proof = view.receipts.get(control_key("sdk-delegation-event", identifier, event_id))
            require(proof is not None
                    and proof.request.read_set == (*_parent_keys(parent), identifier, state_id,
                        *((result_key(identifier),) if result_observation else ()))
                    and proof.result.get("delegation_id") == identifier
                    and proof.result.get("status") in {"unknown", "cancelled"},
                    "cancellation requires both original scoped observations")
        for row_id, link in values.items():
            if not isinstance(link, DelegationModelUsage) or link.delegation_id != identifier:
                continue
            cost_id = cost_key(parent.session_id, link.request_sha256)
            require(link.child_agent_id == state.child_agent_id and link.parent_work_id == parent.execution.work_id
                    and link.root_request_id == parent.root_request_id
                    and link.root_budget_id == parent.execution.owner.root_budget_id
                    and link.session_id == parent.session_id and link.job_id == parent.execution.job_id
                    and link.model_cost_id == cost_id and row_id == usage_key(cost_id)
                    and link.request.request_id == row_id,
                    "child usage escaped the original root")
            cost_reads = (cost_id, parent.registration_id,
                          assistant_key("model-intent", parent.session_id, link.request_sha256))
            _parent_model_cost({key: inventory.rows.get(key) for key in cost_reads}, parent, link.request_sha256)
            written(row_id, link.request, (row_id,))
            checked.add(row_id)
    require(set(values) <= checked, "orphan delegation record is not an original child outcome")
    return values


class DelegationLedger:
    def __init__(self, store: BehaviorStore):
        self.store = store

    def prepare(self, parent: DelegationParent, tool_call_id: str, arguments: dict) -> DelegationReservation:
        parent = _checked(parent, DelegationParent)
        try:
            task = DelegationTask.model_validate(arguments)
        except ValidationError:
            raise Problem(422, "DELEGATION_TASK_INVALID",
                          "only the configured synchronous child is allowed; model and other overrides are forbidden") from None
        request = _request(parent, tool_call_id, digest(task.model_dump(mode="json")))
        intent = DelegationIntent(delegation_id=request.request_id, parent=parent, tool_call_id=tool_call_id,
                                  input_sha256=digest(task.model_dump(mode="json")), request=request)
        state = DelegationState(delegation_id=intent.delegation_id, request=request)
        fresh = False

        def decide(rows):
            nonlocal fresh
            _current_parent(rows, parent)
            if rows[intent.delegation_id] is not None or rows[state_key(intent.delegation_id)] is not None:
                raise Problem(409, "DELEGATION_ORIGINAL_REQUIRED", "read the original delegation; never create another child")
            fresh = True
            return ControlChanges(writes=(
                _write(intent.delegation_id, intent, "create", material(intent.model_dump(mode="json"))),
                _write(state_key(intent.delegation_id), state, "create"),
            ), result={"delegation_id": intent.delegation_id, "status": "unknown"})

        self.store.transact(request, decide)
        return DelegationReservation(fresh=fresh, view=self.read(parent))

    def _record(self, parent, tool_call_id, child_agent_id, event_id, payload, change, *, extra=()):
        parent = _checked(parent, DelegationParent)
        original = self.read(parent)
        identifier = original.delegation_id
        if original.tool_call_id != tool_call_id:
            raise Problem(409, "DELEGATION_SCOPE_CHANGED", "only the original task tool call may be observed")
        request = ControlRequest(
            request_id=control_key("sdk-delegation-event", identifier, event_id),
            request_digest=digest([parent.model_dump(mode="json"), tool_call_id, child_agent_id, payload]),
            read_set=(*_parent_keys(parent), identifier, state_key(identifier), *extra),
        )

        def decide(rows):
            _current_parent(rows, parent, executing=False)
            intent = _row(rows, identifier, DelegationIntent)
            state = _row(rows, state_key(identifier), DelegationState)
            if intent.parent != parent or intent.tool_call_id != tool_call_id:
                raise Problem(409, "DELEGATION_SCOPE_CHANGED", "original parent and task changed")
            if child_agent_id is not None and state.child_agent_id is not None and state.child_agent_id != child_agent_id:
                raise Problem(409, "DELEGATION_CHILD_CHANGED", "a later child cannot replace the original child")
            updated, writes = change(state, rows, request)
            return ControlChanges(writes=(
                _write(state_key(identifier), _updated(updated, request=request), "replace"), *writes,
            ), result={"delegation_id": identifier, "status": updated.status,
                       "failure_code": updated.failure_code})

        self.store.transact(request, decide)
        return self.read(parent)

    def record_started(self, parent, tool_call_id, child_agent_id, *, event_id):
        event = _observation(_Observation, event_id=event_id, tool_call_id=tool_call_id, child_agent_id=child_agent_id)
        if event.child_agent_id is None:
            raise Problem(422, "DELEGATION_OBSERVATION_INVALID", "the started child's original identity is required")
        def change(state, rows, request):
            if state.status not in {"unknown", "running"}:
                raise Problem(409, "DELEGATION_TERMINAL", "a terminal child cannot be started again")
            if state.child_cancelled_event_id is not None:
                raise Problem(409, "DELEGATION_TERMINAL", "a cancelled child cannot be started again")
            return _updated(state, status="unknown" if state.cancel_request_id is not None else "running",
                            child_agent_id=child_agent_id, failure_code=state.failure_code
                            if state.cancel_request_id is not None else None), ()
        return self._record(parent, tool_call_id, child_agent_id, event_id, {"event": "started"}, change)

    def record_unknown(self, parent, tool_call_id, *, event_id, reason_code, child_agent_id=None):
        event = _observation(_UnknownObservation, event_id=event_id, tool_call_id=tool_call_id,
                             child_agent_id=child_agent_id, reason_code=reason_code)
        def change(state, rows, request):
            if state.status not in {"unknown", "running"}:
                raise Problem(409, "DELEGATION_TERMINAL", "uncertain transport cannot replace a saved child outcome")
            return _updated(state, status="unknown", child_agent_id=child_agent_id or state.child_agent_id,
                            failure_code=reason_code), ()
        return self._record(parent, tool_call_id, child_agent_id, event_id,
                            {"event": "unknown", **event.model_dump(mode="json")}, change)

    def request_cancel(self, parent, *, request_id):
        original = self.read(parent)
        event = _observation(_Observation, event_id=request_id, tool_call_id=original.tool_call_id)
        def change(state, rows, request):
            if state.status not in {"unknown", "running"}:
                raise Problem(409, "DELEGATION_TERMINAL", "read the existing terminal child; cancellation cannot replace it")
            return _updated(state, status="unknown",
                            cancel_request_id=state.cancel_request_id or event.event_id,
                            failure_code="DELEGATION_STOP_PENDING"), ()
        return self._record(parent, original.tool_call_id, None,
                            control_key("sdk-delegation-cancel", event.event_id),
                            {"event": "cancel_requested", "request_id": event.event_id}, change)

    def record_parent_idle(self, parent, tool_call_id, child_agent_id, *, event_id):
        event = _observation(_Observation, event_id=event_id, tool_call_id=tool_call_id, child_agent_id=child_agent_id)
        def change(state, rows, request):
            if (state.status not in {"unknown", "running"} or state.child_agent_id is None
                    or event.child_agent_id != state.child_agent_id
                    or state.cancel_request_id is None and state.child_cancelled_event_id is None):
                raise Problem(409, "DELEGATION_STOP_SCOPE", "idle must follow this original child's cancellation")
            stopped = state.child_cancelled_event_id is not None
            return _updated(state, parent_idle_event_id=event.event_id,
                            status="cancelled" if stopped else "unknown",
                            failure_code=None if stopped else "DELEGATION_STOP_PENDING"), ()
        return self._record(parent, tool_call_id, child_agent_id, event_id,
                            {"event": "parent_idle"}, change)

    def record_result(self, parent, tool_call_id, child_agent_id, *, event_id, outcome, text,
                      ended_in_tool_call=False, failure_code=None):
        event = _observation(_ResultObservation, event_id=event_id, tool_call_id=tool_call_id,
                             child_agent_id=child_agent_id, outcome=outcome, text=text,
                             ended_in_tool_call=ended_in_tool_call, failure_code=failure_code)
        packed = pack_delegation_text(event.text)
        identifier = delegation_key(parent.execution.work_id)
        key = result_key(identifier)
        result_sha256 = None if packed is None else packed.sha256
        payload = {"event": "result", "outcome": event.outcome, "sha256": result_sha256,
                   "byte_length": None if packed is None else packed.byte_length,
                   "ended_in_tool_call": event.ended_in_tool_call, "failure_code": event.failure_code}
        incomplete = event.outcome == "completed" and (event.ended_in_tool_call or not event.text or not event.text.strip())
        status = "incomplete" if incomplete else event.outcome
        public_failure = "DELEGATION_RESULT_INCOMPLETE" if incomplete else event.failure_code

        def change(state, rows, request):
            if (state.status not in {"unknown", "running"} or rows[key] is not None
                    or state.child_cancelled_event_id is not None):
                raise Problem(409, "DELEGATION_TERMINAL", "only the original running child may save its result")
            writes = ()
            if packed is not None:
                result = DelegationResult(delegation_id=identifier, child_agent_id=child_agent_id,
                                          data=packed.data, sha256=packed.sha256, byte_length=packed.byte_length, request=request)
                writes = (_write(key, result, "create"),)
            cancelled = event.outcome == "cancelled"
            stopped = cancelled and state.parent_idle_event_id is not None
            final_status = ("cancelled" if stopped else "unknown") if cancelled else status
            final_failure = (None if stopped else "DELEGATION_STOP_PENDING") if cancelled else public_failure
            return _updated(state, status=final_status, child_agent_id=child_agent_id,
                            result_id=key if writes else None, failure_code=final_failure,
                            child_cancelled_event_id=event.event_id if cancelled else None), writes

        return self._record(parent, tool_call_id, child_agent_id, event_id, payload, change, extra=(key,))

    def link_model_request(self, parent, tool_call_id, child_agent_id, request_sha256):
        from .behavior_assistant import key
        from .behavior_instruction_cost import cost_key
        original = self.read(parent)
        identifier = original.delegation_id
        if original.tool_call_id != tool_call_id or original.child_agent_id != child_agent_id:
            raise Problem(409, "DELEGATION_USAGE_SCOPE", "the original task and child must own the model request")
        cost_id = cost_key(parent.session_id, request_sha256)
        row_id = usage_key(cost_id)
        reads = (*_parent_keys(parent), identifier, state_key(identifier), cost_id,
                 key("model-intent", parent.session_id, request_sha256), row_id)
        request = ControlRequest(request_id=row_id,
            request_digest=digest([parent.model_dump(mode="json"), child_agent_id, request_sha256]),
            read_set=reads)
        linked = DelegationModelUsage(
            delegation_id=identifier, child_agent_id=child_agent_id, parent_work_id=parent.execution.work_id,
            root_request_id=parent.root_request_id, root_budget_id=parent.execution.owner.root_budget_id,
            session_id=parent.session_id, job_id=parent.execution.job_id, request_sha256=request_sha256,
            model_cost_id=cost_id, request=request,
        )

        def decide(rows):
            _current_parent(rows, parent, executing=False)
            intent = _row(rows, identifier, DelegationIntent)
            state = _row(rows, state_key(identifier), DelegationState)
            if intent.parent != parent or state.child_agent_id != child_agent_id:
                raise Problem(409, "DELEGATION_USAGE_SCOPE", "parent or child changed before usage association")
            _parent_model_cost(rows, parent, request_sha256)
            if rows[row_id] is not None:
                raise Problem(409, "DELEGATION_USAGE_SCOPE", "a paid model request already has its original association")
            return ControlChanges(writes=(_write(row_id, linked, "create",
                                                 material(linked.model_dump(mode="json"))),),
                                  result={"delegation_id": identifier, "model_cost_id": cost_id,
                                          "root_budget_id": parent.execution.owner.root_budget_id,
                                          "new_charge": False})

        self.store.transact(request, decide)
        self.read_model_usage(parent, request_sha256)
        return linked

    def read_model_usage(self, parent, request_sha256):
        from .behavior_assistant import key
        from .behavior_instruction_cost import cost_key
        from .behavior_runtime_transition import _payload_hash
        parent = _checked(parent, DelegationParent)

        def read():
            delegation = self._read(parent)
            cost_id = cost_key(parent.session_id, request_sha256)
            row_id = usage_key(cost_id)
            rows = self.store.read_snapshot((row_id, cost_id, parent.registration_id,
                                            key("model-intent", parent.session_id, request_sha256)))
            link = _row(rows, row_id, DelegationModelUsage)
            if (link.delegation_id != delegation.delegation_id or link.child_agent_id != delegation.child_agent_id
                    or link.parent_work_id != parent.execution.work_id or link.root_request_id != parent.root_request_id
                    or link.root_budget_id != parent.execution.owner.root_budget_id
                    or link.session_id != parent.session_id or link.job_id != parent.execution.job_id
                    or link.request_sha256 != request_sha256 or link.model_cost_id != cost_id
                    or link.request.request_id != row_id):
                raise Problem(409, "DELEGATION_USAGE_SCOPE", "the retained model association belongs to another root")
            receipt = self.store.reconcile(link.request)
            if dict(receipt.payload["writes"]).get(row_id) != _payload_hash(rows[row_id].payload):
                raise Problem(409, "DELEGATION_RECORD_INVALID", "the original model association receipt is required")
            return _parent_model_cost(rows, parent, request_sha256)

        return self.store.consistent_read(read)

    def read(self, parent: DelegationParent) -> DelegationView:
        parent = _checked(parent, DelegationParent)
        return self.store.consistent_read(lambda: self._read(parent))

    def _read(self, parent):
        from .behavior_runtime_transition import _payload_hash
        identifier = delegation_key(parent.execution.work_id)
        rows = self.store.read_snapshot((identifier, state_key(identifier)))
        if rows[identifier] is None or rows[state_key(identifier)] is None:
            raise Problem(503, "DELEGATION_UNKNOWN", "the original delegation is not confirmed; do not spawn again")
        intent = _row(rows, identifier, DelegationIntent)
        state = _row(rows, state_key(identifier), DelegationState)
        if (intent.parent != parent or intent.delegation_id != identifier or state.delegation_id != identifier
                or intent.request != _request(parent, intent.tool_call_id, intent.input_sha256)):
            raise Problem(409, "DELEGATION_SCOPE_CHANGED", "the original parent and delegation identity must match")
        for key, request in ((identifier, intent.request), (state_key(identifier), state.request)):
            receipt = self.store.reconcile(request)
            if dict(receipt.payload["writes"]).get(key) != _payload_hash(rows[key].payload):
                raise Problem(409, "DELEGATION_RECORD_INVALID", "the original exact write receipt is required")
        text = None
        if state.result_id is not None:
            if state.result_id != result_key(identifier):
                raise Problem(409, "DELEGATION_RESULT_INVALID", "child result reference differs")
            result_rows = self.store.read_snapshot((state.result_id,))
            result = _row(result_rows, state.result_id, DelegationResult)
            receipt = self.store.reconcile(result.request)
            if (result.delegation_id != identifier or result.child_agent_id != state.child_agent_id
                    or dict(receipt.payload["writes"]).get(state.result_id)
                    != _payload_hash(result_rows[state.result_id].payload)):
                raise Problem(409, "DELEGATION_RESULT_INVALID", "child result lacks its exact original receipt")
            text = _result_text(result)
        return DelegationView(delegation_id=identifier, parent=parent, tool_call_id=intent.tool_call_id,
                              input_sha256=intent.input_sha256, status=state.status,
                              child_agent_id=state.child_agent_id, result=text, failure_code=state.failure_code,
                              cancel_requested=state.cancel_request_id is not None,
                              child_cancelled=state.child_cancelled_event_id is not None,
                              parent_idle=state.parent_idle_event_id is not None,
                              stop_confirmed=state.status == "cancelled")
