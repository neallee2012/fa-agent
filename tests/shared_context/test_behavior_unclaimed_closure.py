"""Actual unclaimed fence/worker/delivery producers; all transports offline."""
import json
import copy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ServiceRequestError

from shared_context import behavior_delivery as d, behavior_wire as wire, behavior_workers as w
from shared_context.behavior_assistant import key as assistant_key
from shared_context.behavior_foundry import native_key
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_operations import control_key
from shared_context.behavior_recovery_history import validate_unclaimed_history
from shared_context.behavior_store import ControlRequest
from shared_context.behavior_runtime_rotation_history import validate_retirement
from shared_context.errors import Problem
from shared_context.records import Record
from test_behavior_rotation_normal_pull import changed_inventory
from test_behavior_store import parse_batch
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled, next_rotation
from test_behavior_runtime_rotation import rotated
from test_m3_auth import jwk_client, signing_key


pytestmark = pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)


def read_current(c):
    before = c.store.read_inventory().entity_sha256
    posts = sum(request.method == "POST" for request in c.transport.requests)
    assert installed_origin(MigrationStore(c.composition.clients[0], c.store._scope), c.config) == c.h.c.h.original
    assert c.store.read_inventory().entity_sha256 == before
    assert sum(request.method == "POST" for request in c.transport.requests) == posts


def worker_pool(c):
    key = w.worker_key("pool", c.config.workers.workspace_id)
    return w._Pool.model_validate_json(json.dumps(c.store.read_snapshot((key,))[key].payload["data"]))


def close_unclaimed(c):
    workers = c.native.workers
    binding = workers.inspect(c.binding.job_id).binding
    workers.cancel(c.binding.job_id, binding.owner, expected_binding=binding)
    read_current(c)
    evidence = c.native.fence_unclaimed(binding)
    read_current(c)
    workers.observe(c.binding.job_id, evidence, binding.owner)
    read_current(c)
    workers.forward_result(c.binding.job_id, binding.owner, c.composition.runtime.dep.delivery)
    read_current(c)
    return binding, evidence


def normal_report(c):
    runtime, b = c.composition.runtime, c.binding
    d.DeliveryConsumer(runtime.dep.delivery).consume(b.obligation_id, b.owner, "original-result-only")
    received = runtime.fetch(b.binding_id, wire.Command(request_id="original-cancel-report"), c.principal)
    read_current(c)
    acknowledged = runtime.acknowledge(b.binding_id, wire.PullAcknowledgment(
        request_id="original-cancel-report-ack", exchange_id=received.exchange_id,
        acknowledgment_token=received.acknowledgment_token,
        binding=wire.DeliveryBinding.model_validate_json(received.binding.model_dump_json())), c.principal)
    read_current(c)
    assert acknowledged.payload["result"]["fulfillment"] == "fulfilled"
    return received, acknowledged


def test_true_unclaimed_cancel_report_and_retirement_needs_no_provider_stop(native_acquire):
    c = native_acquire
    sid = assistant_key("session-id", c.starts[0].binding.start_request_id)
    before = c.store.read_inventory()
    assert c.native.launch(c.launch_id).phase == "post_unknown"
    binding, evidence_id = close_unclaimed(c)
    proof = c.native.verify(evidence_id)
    assert proof.result is None and json.loads(proof.proof)["no_provider_stop_claim"] is True
    launch = c.native.launch(c.launch_id)
    assert launch.native_invocation_id is None and launch.claim_id is None and launch.process_id is None
    assert launch.report_token_digest is None and launch.child_pid is None
    received, _ = normal_report(c)
    after = c.store.read_inventory()
    assert assistant_key("session", sid) not in after.rows
    assert assistant_key("acquire", sid, "sdk-start") not in after.receipts
    budget_key = control_key("budget", binding.owner.root_budget_id)
    assert after.rows[budget_key] == before.rows[budget_key]
    grant_key = native_key("grant", launch.grant_id)
    assert after.rows[grant_key] == before.rows[grant_key]
    assert c.requests == [] and len(c.starts) == 1
    _, liability = validate_retirement(after, c.config)
    assert liability["model_outcome"] == "not_requested" and liability["model_request_sha256"] is None
    assert liability["final_delivery_id"] == received.binding.delivery_id
    case = SimpleNamespace(config=c.config, clock=c.clock, store=c.store,
                           first_plan=c.h.plan, h=c.h.c.h)
    successor = next_rotation(case, suffix="-after-unclaimed")
    plan = successor.plan(c.principal, rotation_id="unclaimed-next")
    approval = successor.authorize(c.principal, plan, decision_id="unclaimed-next-approved",
                                    expires_at=c.clock[0] + timedelta(minutes=5))
    successor.apply(c.principal, plan, approval)
    successor.verify(c.principal, plan, approval)
    fresh = MigrationStore(c.composition.clients[0], c.store._scope)
    assert installed_origin(fresh, successor.new) == c.h.c.h.original
    from shared_context.behavior_runtime_rotation import rotation_key
    preserved = fresh.read_inventory()
    changed = {rotation_key("current", c.config.table.scope.authority_domain), *(image.row_id for image in plan.images)}
    assert all(preserved.rows[key] == row for key, row in after.rows.items() if key not in changed)
    assert all(preserved.receipts[key] == row for key, row in after.receipts.items())
    assert c.h.c.first_plan.liability.model_outcome == "unknown"
    assert c.h.c.first_plan.liability.model_request_sha256 == "e" * 64


@pytest.mark.parametrize("cancel_after_fence,lost_forward_reply", [(False, False), (True, False), (False, True)])
def test_unclaimed_version_failure_keeps_current_history_readable(
        native_acquire, monkeypatch, cancel_after_fence, lost_forward_reply):
    c = native_acquire
    workers = c.native.workers
    binding = workers.inspect(c.binding.job_id).binding
    before = c.store.read_inventory()
    observed = str(int(c.native.launch(c.launch_id).target.agent_version) + 1)
    evidence_id = c.native.fence_unclaimed(binding, observed_agent_version=observed)
    read_current(c)
    if cancel_after_fence:
        workers.cancel(c.binding.job_id, binding.owner, expected_binding=binding)
        read_current(c)
        assert c.native.pending_version_observation(workers.inspect(c.binding.job_id))
    evidence = c.native.verify(evidence_id)
    assert evidence.state == "failed" and evidence.result.state == "failed"
    assert json.loads(evidence.result.content)["code"] == "CONVERSATION_SESSION_VERSION_CHANGED"
    if cancel_after_fence:
        workers.stop(c.binding.job_id, binding.owner)
    else:
        workers.observe(c.binding.job_id, evidence_id, binding.owner)
    read_current(c)
    delivery = c.composition.runtime.dep.delivery
    if lost_forward_reply:
        original = delivery.record_work_result
        def lost(command, owner):
            original(command, owner)
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "OFFLINE original result receipt response lost")
        with monkeypatch.context() as patch:
            patch.setattr(delivery, "record_work_result", lost)
            with pytest.raises(Problem) as unknown:
                workers.forward_result(c.binding.job_id, binding.owner, delivery)
            assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
        assert not workers.inspect(c.binding.job_id).result_forwarded
        assert c.native.pending_version_observation(workers.inspect(c.binding.job_id))
        read_current(c)
    workers.forward_result(c.binding.job_id, binding.owner, delivery)
    read_current(c)
    job = workers.inspect(c.binding.job_id)
    assert job.state == "failed" and job.result_forwarded
    assert job.cancel_requested is cancel_after_fence
    assert not c.native.pending_version_observation(job)
    after = c.store.read_inventory()
    budget_key = control_key("budget", binding.owner.root_budget_id)
    grant_key = native_key("grant", c.native.launch(c.launch_id).grant_id)
    assert after.rows[budget_key] == before.rows[budget_key]
    assert after.rows[grant_key] == before.rows[grant_key]
    assert c.requests == [] and len(c.starts) == 1


def claim(c):
    return c.native.claim(c.binding.binding_id, c.launch_id, wire.HostedClaimRequest(
        request_id="late-original-claim", invocation_id="offline-late-invocation", identity=c.identity,
        process_id="offline-late-process", launch_permit=c.permit), c.agent)


@pytest.mark.parametrize("winner", ["claim", "cancel", "claim-races-cancel", "cancel-races-claim"])
def test_late_claim_races_cannot_release_owned_capacity(native_acquire, monkeypatch, winner):
    c, ran = native_acquire, []
    workers, binding = c.native.workers, c.starts[0].binding
    def cancel():
        return workers.cancel(binding.job_id, binding.owner, expected_binding=binding)
    if "-races-" in winner:
        original = c.transport.send
        def send(request, **kwargs):
            if request.method == "POST" and not ran:
                ran.append(True)
                (claim if winner.startswith("claim-") else lambda _: cancel())(c)
            return original(request, **kwargs)
        monkeypatch.setattr(c.transport, "send", send)
        with pytest.raises(Problem) as conflict:
            (cancel if winner.startswith("claim-") else lambda: claim(c))()
        assert conflict.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
        assert ran == [True]
    else:
        (claim if winner == "claim" else lambda _: cancel())(c)
    if winner.startswith("claim"):
        cancel()
        with pytest.raises(Problem) as refused:
            c.native.fence_unclaimed(binding)
        assert refused.value.body["error"]["code"] == "NATIVE_CLAIM_ALREADY_OWNED"
        pool = worker_pool(c)
        assert workers.inspect(binding.job_id).state == "starting"
        assert pool.pending == (binding.job_id,) and any(slot.binding == binding for slot in pool.slots)
        assert c.native.launch(c.launch_id).phase == "claimed"
    else:
        with pytest.raises(Problem):
            claim(c)
        evidence = c.native.fence_unclaimed(binding)
        with pytest.raises(Problem):
            claim(c)
        workers.observe(binding.job_id, evidence, binding.owner)
        assert workers.inspect(binding.job_id).state == "cancelled"
        read_current(c)
    assert c.requests == [] and len(c.starts) == 1


@pytest.mark.parametrize("failure", ["read", "before-commit"])
def test_unknown_fence_failure_is_not_absence_or_capacity_release(native_acquire, monkeypatch, failure):
    c = native_acquire
    workers, binding = c.native.workers, c.starts[0].binding
    workers.cancel(binding.job_id, binding.owner, expected_binding=binding)
    before = c.store.read_inventory()
    original = c.transport.send
    attempts = []
    def send(request, **kwargs):
        if ((failure == "read" and request.method == "GET")
                or (failure == "before-commit" and request.method == "POST")):
            attempts.append(request.method)
            raise ServiceRequestError("synthetic unknown Table transport outcome")
        return original(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    with pytest.raises(Problem):
        c.native.fence_unclaimed(binding)
    assert len(attempts) == 1
    monkeypatch.setattr(c.transport, "send", original)
    assert c.store.read_inventory() == before
    assert workers.inspect(binding.job_id).state == "starting"
    assert any(slot.binding == binding for slot in worker_pool(c).slots)
    assert c.native.launch(c.launch_id).phase == "post_unknown"


def test_exhausted_queries_and_expired_observation_do_not_renew_execution_authority(native_acquire):
    c = native_acquire
    workers, binding = c.native.workers, c.starts[0].binding
    for _ in range(workers.contract.queries_per_generation):
        with pytest.raises(Problem):
            workers.reconcile(binding.job_id, binding.owner)
    assert workers.inspect(binding.job_id).queries_reserved == workers.contract.queries_per_generation
    workers.cancel(binding.job_id, binding.owner, expected_binding=binding)
    first = c.native.fence_unclaimed(binding)
    original = c.store.read_snapshot((native_key("evidence", first),))[native_key("evidence", first)]
    c.clock[0] += timedelta(seconds=61)
    with pytest.raises(Problem):
        c.native.verify(first)
    refreshed = c.native.reconcile_sealed(binding)
    read_current(c)
    workers.observe(binding.job_id, refreshed, binding.owner)
    workers.forward_result(binding.job_id, binding.owner, c.composition.runtime.dep.delivery)
    normal_report(c)
    assert validate_retirement(c.store.read_inventory(), c.config)[1]["model_outcome"] == "not_requested"
    assert c.store.read_snapshot((native_key("evidence", first),))[native_key("evidence", first)] == original
    assert workers.inspect(binding.job_id).queries_reserved == workers.contract.queries_per_generation
    c.clock[0] = c.config.policies[0].policy.valid_until + timedelta(seconds=1)
    with pytest.raises(Problem):
        claim(c)
    with pytest.raises(Problem):
        c.composition.runtime.fetch(c.binding.binding_id, wire.Command(request_id="expired-read"), c.principal)
    read_current(c)
    assert c.requests == [] and len(c.starts) == 1


@pytest.mark.parametrize("lost_at", [
    "cancel", "fence", "observe", "prepare-forward", "work-result", "forwarded", "fetch", "ack", "confirmation",
])
def test_each_durable_boundary_reconciles_original_receipt_without_resending_effect(native_acquire, monkeypatch, lost_at):
    c = native_acquire
    workers, runtime, binding = c.native.workers, c.composition.runtime, c.starts[0].binding
    proof_id = native_key("unclaimed-fence-evidence", c.launch_id)
    effect_id = w.worker_key("stopped-cancel", proof_id)
    ids = {
        "cancel": w.worker_key("cancel-bound", binding.job_id, binding.start_request_id),
        "fence": native_key("fence-unclaimed", c.launch_id),
        "observe": w.worker_key("observe", binding.job_id, proof_id),
        "prepare-forward": w.worker_key("prepare-forward", binding.job_id),
        "work-result": control_key("delivery-request", "work-result", c.binding.obligation_id, effect_id),
        "forwarded": w.worker_key("forwarded", binding.job_id),
    }
    lost, posts = [], []
    original = c.transport.send
    def send(request, **kwargs):
        receipt = None
        if request.method == "POST":
            operations, _ = parse_batch(request)
            receipt = next(json.loads(op[1]["Payload"]) for op in operations
                           if op[1]["RowKey"].startswith("behavior:v1:receipt:"))
            posts.append(receipt["request"]["request_id"])
        response = original(request, **kwargs)
        if receipt is not None and not lost:
            result = receipt["result"]
            matches = (receipt["request"]["request_id"] == ids[lost_at] if lost_at in ids
                       else set(result) == {"exchange_id"} if lost_at == "fetch"
                       else result.get("confirmation_level") == "client_received" if lost_at == "ack"
                       else result.get("fulfillment") == "fulfilled")
            if matches:
                lost.append(ControlRequest.model_validate_json(json.dumps(receipt["request"])))
                raise ServiceRequestError("synthetic committed response loss")
        return response
    monkeypatch.setattr(c.transport, "send", send)
    def durable(action):
        try:
            result = action()
        except Problem as error:
            assert error.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
            assert len(lost) == 1
            count = len(posts)
            c.store.reconcile(lost[0])
            read_current(c)
            assert len(posts) == count
            # Only pure ledger continuation. The real store replays its exact
            # immutable receipt without issuing a second committed request.
            result = action()
        read_current(c)
        return result
    durable(lambda: workers.cancel(binding.job_id, binding.owner, expected_binding=binding))
    evidence = durable(lambda: c.native.fence_unclaimed(binding))
    durable(lambda: workers.observe(binding.job_id, evidence, binding.owner))
    durable(lambda: workers.forward_result(binding.job_id, binding.owner, runtime.dep.delivery))
    d.DeliveryConsumer(runtime.dep.delivery).consume(c.binding.obligation_id, binding.owner, "original-result-only")
    received = durable(lambda: runtime.fetch(c.binding.binding_id, wire.Command(request_id="original-fetch"), c.principal))
    command = wire.PullAcknowledgment(request_id="original-ack", exchange_id=received.exchange_id,
        acknowledgment_token=received.acknowledgment_token,
        binding=wire.DeliveryBinding.model_validate_json(received.binding.model_dump_json()))
    durable(lambda: runtime.acknowledge(c.binding.binding_id, command, c.principal))
    assert len(lost) == 1 and posts.count(lost[0].request_id) == 1
    assert validate_retirement(c.store.read_inventory(), c.config)[1]["model_outcome"] == "not_requested"
    assert c.requests == [] and len(c.starts) == 1


@pytest.mark.parametrize("mutation", [
    "missing-fence", "missing-evidence", "missing-receipt", "receipt-read-set", "receipt-write-set", "receipt-image",
    "proof-material", "proof-provider-stop", "launch-claimed", "launch-checkpoint", "launch-child",
    "binding", "work-content", "result-unknown", "model-intent", "session", "unknown-row", "config",
])
def test_rehashed_unclaimed_history_tampering_cannot_create_retirement_authority(native_acquire, mutation):
    c = native_acquire
    close_unclaimed(c)
    normal_report(c)
    inventory = c.store.read_inventory()
    fence_key = native_key("unclaimed-fence", c.launch_id)
    evidence_key = native_key("evidence", native_key("unclaimed-fence-evidence", c.launch_id))
    receipt_id = native_key("fence-unclaimed", c.launch_id)
    if mutation.startswith("missing-") and mutation != "missing-receipt":
        key = fence_key if mutation == "missing-fence" else evidence_key
        altered = replace(inventory, rows={k: v for k, v in inventory.rows.items() if k != key})
    elif mutation == "missing-receipt":
        altered = changed_inventory(inventory, missing_receipt=receipt_id)
    elif mutation.startswith("receipt-"):
        value = copy.deepcopy(inventory.receipts[receipt_id].payload)
        if mutation == "receipt-read-set":
            value["request"]["read_set"] = value["request"]["read_set"][:-1]
        elif mutation == "receipt-write-set":
            value["writes"] = value["writes"][:-1]
        else:
            value["writes"][0][1] = "0" * 64
        altered = changed_inventory(inventory, receipt_change=(receipt_id, value))
    elif mutation in {"model-intent", "session", "unknown-row"}:
        sid = assistant_key("session-id", c.starts[0].binding.start_request_id)
        key = (assistant_key("model-intent", sid, "e" * 64) if mutation == "model-intent"
               else assistant_key("session", sid) if mutation == "session" else "unknown-late-row")
        value = copy.deepcopy(inventory.rows[fence_key].payload)
        value["row_id"] = key
        value["data"] = {"kind": mutation, "request_sha256": "e" * 64}
        altered = replace(inventory, rows={**inventory.rows, key: Record(value, "test-only")})
    elif mutation == "config":
        with pytest.raises(Problem):
            validate_unclaimed_history(inventory, c.config.model_copy(update={"deployment_id": "wrong-deployment"}))
        return
    else:
        key = (fence_key if mutation.startswith("proof-") else control_key("delivery-work-state", c.binding.obligation_id)
               if mutation in {"work-content", "result-unknown"} else native_key("launch", c.launch_id))
        value = copy.deepcopy(inventory.rows[key].payload)
        if mutation == "proof-material":
            value["material"] = None
        elif mutation == "proof-provider-stop":
            value["data"]["no_provider_stop_claim"] = False
        elif mutation == "binding":
            value["data"]["start"]["binding"]["work_id"] = "unrelated-work"
        elif mutation == "work-content":
            value["data"]["result"]["content"] = "rewritten"
        elif mutation == "result-unknown":
            value["data"]["result"]["unresolved_operations"] = ["real-unknown"]
        else:
            name, new = {"launch-claimed": ("claim_id", "late-claim"),
                         "launch-checkpoint": ("checkpoint", "invented-checkpoint"),
                         "launch-child": ("child_pid", 101)}[mutation]
            value["data"][name] = new
        altered = changed_inventory(inventory, row_changes={key: value})
    with pytest.raises((Problem, ValueError)):
        validate_retirement(altered, c.config)


def test_pending_or_unacknowledged_report_cannot_retire(native_acquire):
    c = native_acquire
    close_unclaimed(c)
    with pytest.raises(Problem):
        validate_retirement(c.store.read_inventory(), c.config)
    runtime, binding = c.composition.runtime, c.binding
    d.DeliveryConsumer(runtime.dep.delivery).consume(binding.obligation_id, binding.owner, "pending-report")
    runtime.fetch(binding.binding_id, wire.Command(request_id="not-acked"), c.principal)
    with pytest.raises(Problem):
        validate_retirement(c.store.read_inventory(), c.config)


@pytest.mark.parametrize("provider_state", ["unknown", "active"])
def test_claim_gate_fence_never_substitutes_for_independent_provider_retirement(native_acquire, provider_state):
    c = native_acquire
    close_unclaimed(c)
    normal_report(c)
    case = SimpleNamespace(config=c.config, clock=c.clock, store=c.store, first_plan=c.h.plan, h=c.h.c.h)
    successor = next_rotation(case, suffix="-provider-not-idle")
    path = case.h.paths.retirement
    retirement = json.loads(path.read_bytes())
    retirement["targets"][0]["provider_session_status"] = provider_state
    path.write_text(json.dumps(retirement), encoding="utf-8")
    before = c.store.read_inventory()
    with pytest.raises(Problem):
        successor.plan(c.principal, rotation_id="still-provider-unknown")
    assert c.store.read_inventory() == before
    assert c.native.launch(c.launch_id).native_invocation_id is None
    assert c.requests == [] and len(c.starts) == 1
