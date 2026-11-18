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
from .behavior_delegation_wire import DelegationPublicText, DelegationStatus, DelegationTask, unpack_delegation_text

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

    def read(self, parent: DelegationParent) -> DelegationView:
        parent = _checked(parent, DelegationParent)
        return self.store.consistent_read(lambda: self._read(parent))

    def _read(self, parent):
        from .source_materials import _payload_hash
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
