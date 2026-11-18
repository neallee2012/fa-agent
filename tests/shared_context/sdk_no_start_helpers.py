"""從 test_sdk_no_start.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import json


def report_packet(c, observed):
    launch_id = c.starts[0].launch_id
    assigned = c.supervisor.journal.assignment(launch_id)
    job = c.h.workers.inspect(c.binding.job_id)
    probe = c.native.probe(job.binding)
    body = {
        "request_id": "original-report", "probe_id": probe.probe_id,
        "invocation_id": c.invocation, "identity": c.identity.model_dump(mode="json"),
        "process_id": assigned.process_id, "claim_id": assigned.claim_id,
        "process_generation": assigned.process_generation, "binding_digest": assigned.binding_digest,
        "supervisor_id": observed.supervisor_id, "sequence": observed.sequence,
        "phase": observed.phase, "outcome": observed.outcome, "child_pid": observed.child_pid,
        "child_exited": observed.child_exited, "exit_code": observed.exit_code,
    }
    if observed.sdk_no_start is not None:
        body["sdk_no_start"] = observed.sdk_no_start.model_dump(mode="json")
    return probe, body
