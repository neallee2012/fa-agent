"""Native claim validation reuses pure reads, but never caches an issuance decision."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import BehaviorStore, GUARD_KEY, _READ_VIEW
from shared_context.behavior_timing import phase_timing
from shared_context.errors import Problem
from test_behavior_store import NativeEntity
from test_conversation_transfer_startup import saved_turn, synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key


@pytest.fixture
def pending_assistant_claim(synthetic_standing_v1):
    c = synthetic_standing_v1
    previous = saved_turn(c, "pure-claim-prior")
    binding, status = c.control.submit(c.human, InstructionSubmit(
        request_id="pure-claim-next", content="Continue the original conversation.",
        conversation_id=previous.assignment.task.instruction.conversation_id,
    ))
    assert status.state == "queued"
    workers = c.h.runtime.dep.workers
    reserved, original = [], workers.adapter
    workers.adapter = replace(original, start=lambda start: reserved.append(c.native.prepare(start)),
                              verify=c.native.verify)
    try:
        workers.start(binding.job_id, binding.owner)
    finally:
        workers.adapter = original
    launch, permit = reserved[0]
    actor = c.h.config.identity.auth().actors[binding.agent_oid]
    agent = BehaviorPrincipal(binding.tenant_id, binding.agent_oid, binding.team_id, "agent", actor.actor,
                              frozenset({"read", "consume", "execute"}))
    identity = wire.HostedIdentity(**{field: getattr(launch.target, field)
                                    for field in ("agent_id", "agent_name", "agent_version", "session_id")})
    claimed = c.native.claim(binding.binding_id, launch.launch_id, wire.HostedClaimRequest(
        request_id="pure-hosted-claim", invocation_id="pure-claim-invocation", identity=identity,
        process_id="synthetic-no-process", launch_permit=permit,
    ), agent)
    command = wire.HostedProbeRequest(
        request_id="pure-assistant-claim", invocation_id=claimed.assignment.invocation_id, identity=identity,
        process_id=claimed.assignment.process_id, claim_id=claimed.assignment.claim_id, report_token=claimed.report_token,
    )
    return SimpleNamespace(c=c, binding=binding, launch=launch, agent=agent, command=command, previous=previous)


def claim(case):
    return case.c.authority.native_claim(case.binding.binding_id, case.launch.launch_id, case.command, case.agent)


def test_claim_validation_reads_fresh_guard_and_installation_at_both_ends(
    pending_assistant_claim, monkeypatch, caplog,
):
    case = pending_assistant_claim
    c = case.c
    reads, issues = [], []
    physical, original_issue = BehaviorStore._physical, c.authority.issue
    installation_key = "behavior:v1:row:" + c.h.runtime.store._installation_fence[0]

    def physical_read(store, key, model, fetched):
        if not issues and key in (GUARD_KEY, installation_key):
            reads.append("guard" if key == GUARD_KEY else "installation")
        return physical(store, key, model, fetched)

    def issue(start):
        assert _READ_VIEW.get() is None, "issuance and its CAS must be outside the pure read view"
        issues.append(start)
        return original_issue(start)

    monkeypatch.setattr(BehaviorStore, "_physical", physical_read)
    monkeypatch.setattr(c.authority, "issue", issue)
    with phase_timing("native-claim-read-view"):
        assignment, ticket = claim(case)
    assert assignment.task.job_id == case.binding.job_id
    assert assignment.task.instruction.previous_archive_id is not None
    assert assignment.sdk_session_id == case.previous.assignment.sdk_session_id
    assert ticket is not None and len(issues) == 1
    assert reads == ["guard", "installation", "guard", "installation"]
    value = next(json.loads(record.getMessage().removeprefix("FA_TIMING "))
                 for record in caplog.records
                 if record.getMessage().startswith('FA_TIMING {"phase":"native-claim-read-view"'))
    print(json.dumps({key: value[key] for key in ("table_gets", "batches")}))


@pytest.mark.parametrize("change,code", [
    ("owner-revoked", "BEHAVIOR_CURRENT_POLICY_DENIED"),
    ("guard-binding", "BEHAVIOR_AUTHORITY_MISMATCH"),
    ("installation", "MIGRATION_INSTALLATION_CHANGED"),
])
def test_changed_validation_cannot_reach_ticket_issuance(pending_assistant_claim, monkeypatch, change, code):
    case = pending_assistant_claim
    c = case.c
    changed, issues = [], []
    original_current, original_issue = c.native._current, c.authority.issue

    def change_authority():
        assert _READ_VIEW.get() is None
        if change == "owner-revoked":
            c.h.mutate(control_key("owner", case.binding.owner.owner_record_id), active=False)
        else:
            store = c.h.runtime.store
            physical_key = (GUARD_KEY if change == "guard-binding" else
                            "behavior:v1:row:" + store._installation_fence[0])
            key = (store._scope.table_name, store._scope.partition_key, physical_key)
            entity = c.h.table.backing.rows[key]
            payload = json.loads(entity["Payload"])
            if change == "guard-binding":
                payload["binding"] = "f" * 64
            else:
                payload["data"]["configuration_sha256"] = "f" * 64
            c.h.table.backing.rows[key] = NativeEntity({
                **entity, "Payload": json.dumps(payload, separators=(",", ":")).encode(),
            }, 'W/"synthetic-authority-change"')
        changed.append(change)

    def checked(rows, launch, **options):
        result = original_current(rows, launch, **options)
        if not changed:
            # A separate writer changes authority after validation, before the view's closing reads.
            with ThreadPoolExecutor(max_workers=1) as worker:
                worker.submit(change_authority).result()
        return result

    def issue(start):
        issues.append(start)
        return original_issue(start)

    monkeypatch.setattr(c.native, "_current", checked)
    monkeypatch.setattr(c.authority, "issue", issue)
    with pytest.raises(Problem) as denied:
        claim(case)
    assert changed == [change]
    assert denied.value.body["error"]["code"] == code
    assert issues == []
    assert _READ_VIEW.get() is None


def test_claim_view_does_not_keep_authority_for_the_next_request(pending_assistant_claim, monkeypatch):
    case = pending_assistant_claim
    c = case.c
    claim(case)
    revoked = c.control.revoke(c.human, "revoked-after-original-claim")
    assert revoked.payload["result"]["revoked"] is True
    issues = []
    original_issue = c.authority.issue

    def issue(start):
        issues.append(start)
        return original_issue(start)

    monkeypatch.setattr(c.authority, "issue", issue)
    with pytest.raises(Problem) as denied:
        claim(case)
    assert denied.value.status == 403
    assert issues == [] and _READ_VIEW.get() is None
