"""One standing native-mode producer flow, no actual SDK, provider or model calls."""
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant import key
from shared_context.behavior_assistant_wire import AssistantSubmit, Answer
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_foundry import native_key
from shared_context.behavior_migration import code_sha256, installed_origin, MigrationStore
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_work_policy_native import (
    retained_handoff, installer, current_machine_evidence, stopped_original,
    original_teams_gap, original_finite_worker_contract, next_window, next_review, original_version,
    bounded_initial_control_quota, closed_v2, original_completed_nonempty_page, installed_empty, operator,
    exact_source_initialization, fixed_original_high_water, expired_claim, source_contract, closed_case,
    offline_socket_guard, original_captured_route_producer, closed_finished, sufficient_initial_budget,
    durable_result, native_acquire, current_rotation, two_rotations, current_cancelled, rotated,
    jwk_client, signing_key,
)

pytestmark = pytest.mark.parametrize("source_contract", ["claim"], indirect=True)


@pytest.fixture(autouse=True)
def no_runtime(monkeypatch):
    import subprocess
    from copilot import CopilotClient
    def forbidden(*args, **kwargs):
        pytest.fail("this closure case must not launch a runtime or provider")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(CopilotClient, "start", forbidden)


@pytest.fixture
def unfinished(stopped_original, tmp_path):
    c, _, _, _ = stopped_original
    handoff = retained_handoff(stopped_original)
    install, facts = installer(c, handoff, tmp_path)
    plan = install.plan(c.principal, installation_id="offline-close-standing")
    approval = install.authorize(c.principal, plan, decision_id="offline-standing",
                                 expires_at=c.clock[0] + timedelta(minutes=4))
    install.apply(c.principal, plan, approval)
    install.source(c.principal, plan, approval)
    c.parent.installation()
    current_machine_evidence(c, install)
    runtime, native = c.runtime, c.runtime.dep.native_launches
    control, workers = runtime.work_policy, runtime.dep.workers
    b, _ = control.submit(c.principal, AssistantSubmit(request_id="offline-original-close"))
    starts = []
    workers.adapter = replace(workers.adapter, start=lambda start: starts.append(native.prepare(start)))
    workers.start(b.job_id, b.owner)
    launch, permit = starts[0]
    identity = wire.HostedIdentity(**{
        name: getattr(launch.target, name) for name in ("agent_id", "agent_name", "agent_version", "session_id")
    })
    actor = BehaviorPrincipal(b.tenant_id, b.agent_oid, b.team_id, "agent", "synthetic-agent",
                             frozenset({"read", "consume", "execute"}))
    claim = native.claim(b.binding_id, launch.launch_id, wire.HostedClaimRequest(
        request_id="offline-claim", invocation_id="offline-original-invocation", identity=identity,
        process_id="offline-process", launch_permit=permit), actor)
    command = wire.HostedProbeRequest(request_id="offline-assistant-claim",
        invocation_id=claim.assignment.invocation_id, identity=identity, process_id=claim.assignment.process_id,
        claim_id=claim.assignment.claim_id, report_token=claim.report_token)
    authority = c.parent.assistant.authority
    assignment, ticket = authority.native_claim(b.binding_id, launch.launch_id, command, actor)
    session = authority.current_session(b.binding_id, assignment.session_id, ticket, actor)
    from shared_context import behavior_assistant_wire as aw
    authority.acquire(session, wire.Command(request_id="sdk-start"))
    # Refresh the original typed session after each committed producer.
    def current():
        return authority.current_session(b.binding_id, assignment.session_id, ticket, actor)
    context = authority.context(current(), wire.Command(request_id="current-context"), actor)
    for number in range(3):
        body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": f"offline-{number}"}],
                "stream": True, "max_tokens": 1024}
        request_hash = digest(body)
        authority.reserve_model(current(), aw.Reservation(request_id=request_hash, request_sha256=request_hash), actor)
        authority.consume_model(current(), aw.ModelRequest(request_id=request_hash, body=body), actor)
    values = context.model_dump()
    answer = Answer(item_count=values["item_count"], completed_count=values["completed_count"],
                    remaining_count=values["item_count"] - values["completed_count"])
    authority.save(current(), aw.Save(request_id="save-result", answer=answer), actor)
    control.cancel(c.principal, b.binding_id)
    protocol = []
    def unknown(command):
        protocol.append(command)
        raise Problem(409, "HOSTED_PROCESS_UNOWNED", "offline reporter unavailable")
    workers.adapter = replace(workers.adapter, stop=unknown)
    with pytest.raises(Problem) as error:
        workers.stop(b.job_id, b.owner)
    assert error.value.body["error"]["code"] == "WORKER_STOP_UNKNOWN"
    assert workers.inspect(b.job_id).stop_credit == 0 and len(protocol) == 1
    c.parent.installation()
    return SimpleNamespace(c=c, b=b, native=native, workers=workers, control=control,
                           assignment=assignment, launch=launch, protocol=protocol)


def test_original_saved_unfinished_stop_closes_atomically_and_reads_back(unfinished, monkeypatch):
    from shared_context.behavior_recovery_close import CloseOriginal, close_original, reconcile_original
    from shared_context.behavior_recovery import PlatformStopObservation, stop_endpoint
    h = unfinished
    c, b = h.c, h.b
    inventory = c.store.read_inventory()
    command = CloseOriginal(
        configuration_sha256=c.config.sha256, code_sha256=code_sha256(),
        job_id=b.job_id, launch_id=h.launch.launch_id, owner=b.owner,
        registration_sha256=digest(h.control.records[b.binding_id].model_dump(mode="json")),
        job_sha256=digest(h.workers.inspect(b.job_id).model_dump(mode="json")),
        invocation_id="offline-original-invocation",
        stop=PlatformStopObservation(target=h.launch.target, endpoint=stop_endpoint(h.launch.target),
            credential_client_id=c.config.identity.managed_identity_client_id, status_code=204,
            observed_at=c.clock[0]),
        retained_receipt_sha256="e" * 64, approval_reference="offline-explicit-close",
    )
    for changed in (
        command.model_copy(update={"job_id": "wrong-job"}),
        command.model_copy(update={"stop": command.stop.model_copy(update={
            "target": command.stop.target.model_copy(update={"session_id": "wrong-session"})})}),
        command.model_copy(update={"job_sha256": "f" * 64}),
    ):
        with pytest.raises(Problem):
            close_original(c.parent, changed, c.principal)
        assert c.store.read_inventory().entity_sha256 == inventory.entity_sha256
    from azure.core.exceptions import ServiceRequestError
    from test_behavior_store import batch_conflict_response, parse_batch
    send, posts = c.transport.send, []
    mode = ["conflict"]
    def transmit(request, **kwargs):
        if request.method == "POST":
            operations, _ = parse_batch(request)
            record = next(json.loads(op[1]["Payload"]) for op in operations
                          if op[1]["RowKey"] == "behavior:v1:receipt:" + command.request_id)
            posts.append(record)
            if mode[0] == "conflict":
                return batch_conflict_response(request, 412)
            response = send(request, **kwargs)
            if mode[0] == "ack-lost":
                raise ServiceRequestError("OFFLINE original committed response lost")
            return response
        return send(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", transmit)
    with pytest.raises(Problem) as conflict:
        close_original(c.parent, command, c.principal)
    assert conflict.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert c.store.read_inventory().entity_sha256 == inventory.entity_sha256
    mode[0] = "ack-lost"
    with pytest.raises(Problem) as unknown:
        close_original(c.parent, command, c.principal)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert unknown.value.body["error"]["request_id"] == command.request_id
    mode[0] = "no-more-close-posts"
    receipt = reconcile_original(c.parent, command, c.principal)
    monkeypatch.setattr(c.transport, "send", send)
    assert len(posts) == 2  # One definite CAS rejection, then one committed/ACK-unknown submission.
    assert receipt.payload["result"]["state"] == "cancelled"
    job = h.workers.inspect(b.job_id)
    assert job.state == "cancelled" and job.stop_credit == job.completion_credit == 0
    assert job.binding.generation == 1 and not job.result_forwarded
    pool = c.store.read_snapshot((worker_key("pool", c.config.workers.workspace_id),))
    assert all(slot["binding"] is None for slot in next(iter(pool.values())).payload["data"]["slots"])
    assert h.native.launch(h.launch.launch_id).phase == "sealed"
    c.parent.installation()
    before_replay = c.store.read_inventory()
    expected_writes = dict(receipt.payload["writes"])
    assert len(expected_writes) == 9
    assert {worker_key("job", b.job_id), worker_key("pool", c.config.workers.workspace_id),
            native_key("launch", h.launch.launch_id), native_key("target", h.launch.target.executor_id),
            command.request_id} <= set(expected_writes)
    assert receipt.payload["guard_revision"] == inventory.guard.payload["revision"] + 1
    assert reconcile_original(c.parent, command, c.principal) == receipt
    assert close_original(c.parent, command, c.principal) == receipt
    assert c.store.read_inventory() == before_replay
    with pytest.raises(Problem):
        reconcile_original(c.parent, command.model_copy(update={"retained_receipt_sha256": "f" * 64}), c.principal)
    assert c.store.read_inventory() == before_replay
    proof = h.native.verify(receipt.payload["result"]["evidence_id"])
    assert proof.result.state == "cancelled" and len(proof.result.unresolved_operations) == 3
    h.workers.forward_result(b.job_id, b.owner, c.runtime.dep.delivery)
    c.parent.installation()
    assert h.workers.inspect(b.job_id).result_forwarded
    assert not c.runtime.job_status(b.binding_id, c.principal).obligation_fulfilled
    from shared_context.behavior_delivery import DeliveryConsumer
    DeliveryConsumer(c.runtime.dep.delivery).consume(b.obligation_id, b.owner, "offline-original-close")
    fetched = c.runtime.fetch(b.binding_id, wire.Command(request_id="offline-cancelled-read"), c.principal)
    assert json.loads(fetched.payload)["work_state"] == "cancelled"
    c.parent.installation()
    after = c.store.read_inventory()
    for name in (key("artifact", b.work.work_id), h.control.cost_key,
                 control_key("budget", b.owner.root_budget_id), key("session", h.assignment.session_id)):
        assert after.rows[name] == inventory.rows[name]
    assert key("result", b.work.work_id) not in after.rows
    assert len(h.protocol) == 1
    print("PASS: original saved/no-finish -> one atomic closure after CAS rejection; ACK unknown reconciled; "
          "same-ID read only; correct current history/forward/fetch; artifact/session/root/USD retained")
