import json

import pytest
from pydantic import ValidationError

from shared_context.behavior_assistant import _Session, key as assistant_key
from shared_context.behavior_recovery import RecoveryScope
from shared_context.behavior_wire import Command
from shared_context.errors import Problem
from test_behavior_recovery import recovery, approve, data
from test_behavior_assistant import assistant
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


pytestmark = pytest.mark.parametrize("recovery", ["pre-sdk"], indirect=True)


def test_explicit_pre_sdk_stop_has_no_invented_model_unknown_or_budget_refund(recovery):
    h, service, principal, calls = recovery
    before = data(h, "budget", "root")
    authorization = approve(service, principal)
    assert authorization.plan.recovery_phase == "before_sdk_acquire"
    assert authorization.plan.model_request_sha256 is None
    result = service.execute(principal, authorization)
    assert result["state"] == "cancelled" and result["model_outcome"] == "not_requested"
    assert len(calls) == 1 and h.endpoint.requests == []
    assert data(h, "budget", "root") == before
    job = h.workers.inspect("job-one")
    assert job.queries_reserved == 16 and job.result_forwarded
    assert data(h, "worker-pool", h.workers.contract.workspace_id)["slots"][0]["binding"] is None
    proof = service.native.verify(result["platform_stop_evidence_id"])
    assert not proof.result.unresolved_operations
    body = json.loads(proof.result.content)
    assert body["model_request_sha256"] is None and body["model_outcome"] == "not_requested"
    assert service.native.launch(service.scope.launch_id).child_pid is None
    assert service.execute(principal, authorization)["stop_resent"] is False
    assert len(calls) == 1


def test_legacy_recovery_schema_still_requires_an_actual_model_digest(recovery):
    _, service, _, _ = recovery
    value = service.scope.model_dump(mode="json")
    value.pop("recovery_phase")
    with pytest.raises(ValidationError):
        RecoveryScope.model_validate(value)


@pytest.mark.parametrize("change", [
    {"state": "running"},
    {"state": "waiting", "reason": "sdk_failed"},
    {"context_read": True},
    {"model_requests": 1},
])
def test_pre_sdk_permission_cannot_cover_later_execution_state(recovery, change):
    h, service, principal, calls = recovery
    job = h.workers.inspect("job-one")
    sid = assistant_key("session-id", job.binding.start_request_id)
    h.mutate(assistant_key("session", sid), **change)
    with pytest.raises(Problem) as error:
        service.plan(principal)
    assert error.value.body["error"]["code"] == "RECOVERY_ORIGINAL_CHANGED"
    assert not calls


def test_acquire_receipt_cannot_be_hidden_by_an_accepted_session_pointer(recovery):
    h, service, principal, calls = recovery
    sid = assistant_key("session-id", h.workers.inspect("job-one").binding.start_request_id)
    key = assistant_key("session", sid)
    session = _Session.model_validate(h.store.read_snapshot((key,))[key].payload["data"])
    h.assistant.acquire(session, Command(request_id="sdk-start"))
    h.mutate(key, state="accepted")
    with pytest.raises(Problem) as error:
        service.plan(principal)
    assert error.value.body["error"]["code"] == "RECOVERY_ORIGINAL_CHANGED"
    assert not calls


def test_pre_sdk_stop_unknown_keeps_original_slot_and_never_repeats_stop(recovery):
    h, service, principal, calls = recovery
    authorization = approve(service, principal)

    def unknown(target, deadline):
        calls.append(target)
        raise TimeoutError("synthetic opaque stop error")

    service.stop = unknown
    with pytest.raises(Problem) as error:
        service.execute(principal, authorization)
    assert error.value.body["error"]["code"] == "RECOVERY_STOP_UNKNOWN"
    assert data(h, "worker-pool", h.workers.contract.workspace_id)["slots"][0]["binding"] is not None
    assert service.execute(principal, authorization)["stop-observation"] is False
    assert len(calls) == 1


def test_unavailable_receipt_read_is_not_treated_as_proven_absence(recovery, monkeypatch):
    h, service, principal, calls = recovery
    read = h.store._read

    def unavailable(key, model, *fetched):
        if key.startswith("behavior:v1:receipt:"):
            raise Problem(503, "BEHAVIOR_READ_UNAVAILABLE", "synthetic failure")
        return read(key, model, *fetched)

    monkeypatch.setattr(h.store, "_read", unavailable)
    with pytest.raises(Problem) as error:
        service.plan(principal)
    assert error.value.body["error"]["code"] == "BEHAVIOR_READ_UNAVAILABLE"
    assert not calls and not h.workers.inspect("job-one").cancel_requested
