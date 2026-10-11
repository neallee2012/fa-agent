"""The trusted operator reads the existing stop record; it never sends a stop."""
import hashlib
import json

from shared_context.behavior_foundry import FoundryTarget


def test_retained_sdk_stop_record_binds_exact_original_target():
    from shared_context.behavior_recovery_close import read_stop_receipt
    target = FoundryTarget(executor_id="executor-one",
        project_endpoint="https://synthetic.services.ai.azure.com/api/projects/p",
        agent_id="11111111-1111-1111-1111-111111111111", agent_name="assistant",
        agent_version="29", session_id="original-owned-session",
        agent_oid="22222222-2222-2222-2222-222222222222")
    calls = [{"method": "GET", "action": "original-status", "status_code": 200, "request_id": None},
             {"method": "POST", "action": "stop-session", "status_code": 204, "request_id": None}]
    record = {"records": {
        "standing_normal_platform_stop_receipt": {
            "kind": "standing_normal_platform_stop_receipt", "at": "2026-01-01T00:00:00Z",
            "job_id": "job-one", "original_invocation": "inv-one",
            "session_id": target.session_id, "stop_returned_204": True,
            "calls": calls, "automatic_retry": False,
        },
        "standing_normal_platform_stop": {
            "outcome": "STOP_RETURNED_204", "job_id": "job-one", "original_invocation": "inv-one",
            "before": {"agent_session_id": target.session_id,
                       "version_indicator": {"type": "version_ref", "agent_version": "29"}, "status": "active"},
            "after": {"agent_session_id": target.session_id,
                      "version_indicator": {"type": "version_ref", "agent_version": "29"}, "status": "idle"},
            "calls": calls + [{"method": "GET", "action": "original-status", "status_code": 200, "request_id": None}],
        },
    }}
    raw = json.dumps(record).encode()
    stop, source_hash = read_stop_receipt(raw, job_id="job-one", invocation_id="inv-one",
                                         target=target, credential_client_id="original-client")
    assert stop.status_code == 204 and stop.target == target
    assert stop.provider_request_id is None
    assert source_hash == hashlib.sha256(raw).hexdigest()
