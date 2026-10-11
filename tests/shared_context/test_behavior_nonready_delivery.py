"""Approved C1 nonready delivery seam; actual producers, never cloud activity."""
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from shared_context.behavior_delivery import DeliveryConsumer
from shared_context.behavior_operations import control_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_server import run_controller
from shared_context.behavior_server_config import SupervisorLimits
from shared_context.errors import Problem
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled
from test_behavior_runtime_rotation import rotated
from test_m3_auth import jwk_client, signing_key
from test_behavior_settlement import write
from test_behavior_second_model_conflicts import controller_write
from test_behavior_assistant_claim_boundaries import hosted_flow, reviewed
from test_behavior_store import parse_batch


def drain(c, *, pending=True, binding=None):
    runtime = c.composition.runtime
    return DeliveryConsumer(runtime.dep.delivery).drain(
        c.binding.owner.root_budget_id, runtime.resolve_owner, c.config.controller_id, limit=1,
        **({"pending_binding": binding or c.binding} if pending else {}))


@pytest.fixture
def adequate_original_scans(monkeypatch):
    import test_behavior_migration as migration
    import test_behavior_runtime_transition as transition
    from shared_context.behavior_server_config import ServerConfiguration
    original, quota_type = transition.synthetic_configuration, migration.QuotaRecord
    def configuration():
        raw = original().model_dump(mode="json")
        raw["workers"].update(queries_per_generation=16, control_units=16)
        for root in raw["roots"]:
            root["scans"] = 1000
        return ServerConfiguration.model_validate_json(json.dumps(raw))
    def quota(**fields):
        if fields.get("scope_kind") == "root" and fields.get("scans_remaining") == 3:
            fields["scans_remaining"] = 1000
        return quota_type(**fields)
    monkeypatch.setattr(transition, "synthetic_configuration", configuration)
    monkeypatch.setattr(migration, "QuotaRecord", quota)


def test_actual_controller_does_not_write_nonready_delivery_pages(native_acquire):
    c = native_acquire
    runtime = c.composition.runtime
    quota = control_key("delivery-quota", "root", c.binding.owner.root_budget_id)
    before = c.store.read_snapshot((quota,))[quota]
    queries = []
    def query(binding):
        queries.append(binding)
        c.native.probe(binding)
        raise Problem(503, "NATIVE_QUERY_UNKNOWN", "synthetic original observation remains unresolved")
    runtime.dep.workers.adapter = replace(runtime.dep.workers.adapter, query=query)
    result = run_controller(c.composition, limits=SupervisorLimits(ticks=2, page_limit=1, interval_seconds=0.0))
    assert result["ticks"] == 2 and len(queries) == 2
    assert runtime.dep.workers.inspect(c.binding.job_id).queries_reserved == 2
    assert c.native.launch(c.launch_id).probes_reserved == 2
    assert c.store.read_snapshot((quota,))[quota] == before
    assert result["delivery_items"] == 0
    assert result["problem_counts"] == {"WORKER_QUERY_UNKNOWN": 2}


def test_defer_is_a_complete_read_only_decision_and_default_drain_is_unchanged(native_acquire):
    c = native_acquire
    before = c.store.read_inventory()
    assert drain(c) == ()
    assert c.store.read_inventory() == before
    results = drain(c, pending=False)
    assert results[0]["problem"] == "DELIVERY_RESULT_PENDING"
    after = c.store.read_inventory()
    assert after.guard.payload["revision"] == before.guard.payload["revision"] + 2
    quota = control_key("delivery-quota", "root", c.binding.owner.root_budget_id)
    assert after.rows[quota].payload["data"]["scans_remaining"] == before.rows[quota].payload["data"]["scans_remaining"] - 1


@pytest.mark.parametrize("fault", [
    "owner", "epoch", "conversation", "runtime-policy", "expired-policy", "policy-revision", "route",
    "work", "intent", "index-owner", "quota", "pool-binding", "pool-slot", "job-owner", "old-config", "old-schema", "missing",
])
def test_wrong_or_incomplete_authority_never_becomes_nonready(native_acquire, fault):
    from shared_context.behavior_workers import worker_key
    c, b = native_acquire, native_acquire.binding
    row = {
        "owner": control_key("owner", b.owner.owner_record_id),
        "epoch": control_key("owner", b.owner.owner_record_id),
        "conversation": control_key("delivery-conversation", b.owner.conversation_id),
        "runtime-policy": runtime_key("policy", b.binding_id),
        "expired-policy": runtime_key("policy", b.binding_id),
        "policy-revision": control_key("policy", b.owner.policy_id),
        "route": control_key("delivery-obligation", b.obligation_id),
        "work": control_key("delivery-work-state", b.obligation_id),
        "intent": control_key("delivery-intent", control_key("delivery-intent-id", b.event_id)),
        "index-owner": control_key("delivery-index", b.owner.root_budget_id),
        "quota": control_key("delivery-quota", "root", b.owner.root_budget_id),
        "pool-binding": worker_key("pool", c.config.workers.workspace_id),
        "pool-slot": worker_key("pool", c.config.workers.workspace_id),
        "job-owner": worker_key("job", b.job_id),
        "old-config": control_key("delivery-conversation", b.owner.conversation_id),
        "old-schema": control_key("delivery-work-state", b.obligation_id),
        "missing": control_key("delivery-conversation", b.owner.conversation_id),
    }[fault]
    data = json.loads(json.dumps(c.store.read_snapshot((row,))[row].payload["data"]))
    if fault in {"owner", "runtime-policy"}:
        data["active"] = False
    elif fault in {"epoch", "conversation"}:
        data["epoch"] += 1
    elif fault == "expired-policy":
        data["valid_until"] = c.clock[0].isoformat()
    elif fault == "policy-revision":
        data["revision"] += 1
    elif fault == "route":
        data["route"]["recipient_id"] = "wrong-recipient"
    elif fault == "work":
        data["definition"]["goal"] = "wrong-original-work"
    elif fault == "intent":
        data["consumer_version"] = "wrong-consumer"
    elif fault == "index-owner":
        data["entries"][0]["owner_record_id"] = "foreign-owner"
    elif fault == "quota":
        data["pending"] += 1
    elif fault == "pool-binding":
        data["slots"][0]["binding"]["work_id"] = "foreign-work"
    elif fault == "pool-slot":
        data["slots"][0]["number"] = 1
    elif fault == "job-owner":
        data["owner"]["epoch"] += 1
    elif fault == "old-config":
        data["configuration_digest"] = "f" * 64
    elif fault == "old-schema":
        data["schema_version"] = 0
    if fault == "missing":
        c.transport.backing.rows.pop(("Behavior", c.store._scope.partition_key, "behavior:v1:row:" + row))
    else:
        write(c.competitor, row, data)
    before = c.store.read_inventory()
    with pytest.raises(Problem):
        drain(c)
    assert c.store.read_inventory() == before


@pytest.mark.parametrize("fault", ["read", "guard", "readiness", "readiness-expired", "time-policy"])
def test_read_or_clock_failure_is_not_a_defer_decision(native_acquire, monkeypatch, fault):
    c, runtime = native_acquire, native_acquire.composition.runtime
    core = runtime.dep.delivery
    if fault == "read":
        def unavailable(_):
            raise Problem(503, "BEHAVIOR_READ_FAILED", "synthetic unavailable")
        monkeypatch.setattr(core.store, "read_snapshot", unavailable)
    elif fault in {"readiness", "readiness-expired"}:
        original = core.registration.verify_readiness
        def readiness():
            proof = original()
            return proof.model_copy(update={"coverage": ()} if fault == "readiness" else {"expires_at": c.clock[0]})
        core.registration = replace(core.registration, verify_readiness=readiness)
    else:
        original = core.store._guard
        changed = []
        def guard():
            result = original()
            if not changed:
                changed.append(True)
                if fault == "guard":
                    c.native.probe(c.starts[0].binding)
                else:
                    c.clock[0] = c.config.policies[0].policy.valid_until
            return result
        monkeypatch.setattr(core.store, "_guard", guard)
    with pytest.raises(Problem) as denied:
        drain(c)
    if fault == "guard":
        assert denied.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"


@pytest.mark.parametrize("state", ["deadline", "budget-zero", "budget-inactive", "active-page", "another-pending", "cancel"])
def test_nondeferrable_work_uses_original_drain(native_acquire, monkeypatch, state):
    from shared_context.behavior_store import ControlRequest
    from shared_context.source_materials import digest
    c, b = native_acquire, native_acquire.binding
    quota = control_key("delivery-quota", "root", b.owner.root_budget_id)
    if state == "deadline":
        k = control_key("delivery-obligation", b.obligation_id)
        value = c.store.read_snapshot((k,))[k].payload["data"]
        write(c.competitor, k, {**value, "deadline": c.clock[0].isoformat()})
    elif state in {"budget-zero", "budget-inactive"}:
        k = control_key("budget", b.owner.root_budget_id)
        value = c.store.read_snapshot((k,))[k].payload["data"]
        write(c.competitor, k, {**value, **({"remaining_units": 0} if state == "budget-zero" else {"active": False})})
    elif state == "cancel":
        c.native.workers.cancel(b.job_id, b.owner, expected_binding=c.starts[0].binding)
    elif state == "another-pending":
        k = control_key("delivery-index", b.owner.root_budget_id)
        value = c.store.read_snapshot((k,))[k].payload["data"]
        entry = {**value["entries"][0], "obligation_id": "zz-another-pending"}
        write(c.competitor, k, {**value, "entries": [*value["entries"], entry]})
        q = c.store.read_snapshot((quota,))[quota].payload["data"]
        write(c.competitor, quota, {**q, "pending": q["pending"] + 1})
    else:
        original = c.store.transact
        saved = []
        def lost(request, decide, **kwargs):
            receipt = original(request, decide, **kwargs)
            if "page" in receipt.payload["result"] and not saved:
                saved.append(request)
                raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "synthetic scan response lost")
            return receipt
        monkeypatch.setattr(c.store, "transact", lost)
        with pytest.raises(Problem):
            drain(c, pending=False)
        monkeypatch.setattr(c.store, "transact", original)
        assert len(saved) == 1
        c.store.reconcile(saved[0])
    before = c.store.read_snapshot((quota,))[quota].payload["data"]["scans_remaining"]
    result = drain(c)
    after = c.store.read_snapshot((quota,))[quota].payload["data"]["scans_remaining"]
    assert result != ()
    assert after == before - (0 if state == "active-page" else 1)
    if state in {"deadline", "budget-zero", "budget-inactive"}:
        assert result[0]["status"] == "awaiting_client_ack"
    else:
        assert result[0]["problem"] == "DELIVERY_RESULT_PENDING"


@pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)
def test_terminal_result_after_snapshot_is_seen_next_tick_and_pending_ack_is_not_deferred(hosted_flow, monkeypatch):
    h, c = hosted_flow, hosted_flow.c
    h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    h.supervisor.join(timeout=70)
    assert h.supervisor.observe(c.launch_id).phase == "sealed"
    assert c.native.workers.inspect(c.binding.job_id).state == "starting"
    original, original_work = c.store._guard, DeliveryConsumer._original_work
    armed, arrived = [], []
    def work(consumer, *args):
        result = original_work(consumer, *args)
        armed.append(True)
        return result
    def guard():
        value = original()
        if armed and not arrived:
            arrived.append(True)
            controller_write(h, 2)
            c.native.workers.forward_result(c.binding.job_id, c.binding.owner, c.composition.runtime.dep.delivery)
        return value
    monkeypatch.setattr(DeliveryConsumer, "_original_work", work)
    monkeypatch.setattr(c.store, "_guard", guard)
    assert drain(c) == ()
    assert arrived == [True]
    monkeypatch.setattr(DeliveryConsumer, "_original_work", original_work)
    monkeypatch.setattr(c.store, "_guard", original)
    first = drain(c)
    assert first[0]["status"] == "awaiting_client_ack"
    assert drain(c)[0]["status"] == "awaiting_client_ack"


@pytest.mark.parametrize("change", ["teams", "checksum", "foreign-root", "foreign-binding"])
def test_python_opt_in_cannot_relabel_wrong_binding(native_acquire, change):
    c, binding = native_acquire, native_acquire.binding
    if change == "teams":
        binding = binding.model_copy(update={"route": binding.route.model_copy(update={"channel_id": "msteams"})})
    elif change == "checksum":
        binding = binding.model_copy(update={"work": binding.work.model_copy(update={"required_checks": ("sha256",)})})
    elif change == "foreign-root":
        binding = binding.model_copy(update={"owner": binding.owner.model_copy(update={"root_budget_id": "other-root"})})
    else:
        binding = binding.model_copy(update={"binding_id": "other-binding"})
    with pytest.raises(Problem):
        drain(c, binding=binding)


@pytest.mark.parametrize("time_limit", ["deadline", "policy"])
def test_time_boundary_is_rechecked_after_final_snapshot(native_acquire, monkeypatch, time_limit):
    c = native_acquire
    row_key = control_key("delivery-obligation", c.binding.obligation_id)
    current = c.store.read_snapshot((row_key,))[row_key].payload["data"]
    deadline = c.clock[0] + timedelta(seconds=1)
    write(c.competitor, row_key, {**current, "deadline": deadline.isoformat()})
    original = DeliveryConsumer._original_work
    changed = []
    def checked(consumer, *args):
        result = original(consumer, *args)
        if not changed:
            changed.append(True)
            c.clock[0] = deadline if time_limit == "deadline" else c.config.policies[0].policy.valid_until
        return result
    monkeypatch.setattr(DeliveryConsumer, "_original_work", checked)
    if time_limit == "deadline":
        assert drain(c)[0]["status"] == "awaiting_client_ack"
    else:
        with pytest.raises(Problem) as denied:
            drain(c)
        assert denied.value.body["error"]["code"] == "BEHAVIOR_CURRENT_POLICY_DENIED"
    assert changed == [True]


def test_real_controller_cancel_lane_is_never_deferred_or_resent(native_acquire):
    c = native_acquire
    workers = c.native.workers
    workers.cancel(c.binding.job_id, c.binding.owner, expected_binding=c.starts[0].binding)
    stops = []
    queries = []
    def stop(command):
        stops.append(command)
        raise Problem(503, "NATIVE_STOP_UNKNOWN", "synthetic existing stop outcome remains unknown")
    def query(binding):
        queries.append(binding)
        c.native.probe(binding)
        raise Problem(503, "NATIVE_QUERY_UNKNOWN", "synthetic existing observation remains unresolved")
    workers.adapter = replace(workers.adapter, stop=stop, query=query)
    result = run_controller(c.composition, limits=SupervisorLimits(ticks=2, page_limit=1, interval_seconds=0.0))
    assert result["ticks"] == 2 and len(stops) == 1
    assert len(queries) == 1 and workers.inspect(c.binding.job_id).queries_reserved == 1
    assert workers.inspect(c.binding.job_id).cancel_requested
    assert workers.inspect(c.binding.job_id).stop_credit == 0
    assert workers.inspect(c.binding.job_id).state in {"starting", "capacity_uncertain"}
    assert result["delivery_items"] == 2


@pytest.mark.usefixtures("adequate_original_scans")
@pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)
@pytest.mark.parametrize("necessary", [False, True], ids=["delivery-only", "query-report-and-delivery"])
@pytest.mark.parametrize("opt_in", [False, True], ids=["default", "optimized"])
def test_full_sdk_controller_comparison_preserves_necessary_writers(hosted_flow, monkeypatch, opt_in, necessary):
    h, c = hosted_flow, hosted_flow.c
    consume, transact, transport = c.authority.consume_model, c.store.transact, c.transport.send
    state = {"models": 0, "second": False, "attempt": 0, "controller": False}
    injected, commits = set(), []
    budget_key = control_key("budget", c.binding.owner.root_budget_id)
    quota_key = control_key("delivery-quota", "root", c.binding.owner.root_budget_id)
    before = c.store.read_snapshot((budget_key, quota_key))
    assert before[quota_key].payload["data"]["scans_remaining"] >= 3
    def consume_model(*args):
        state["models"] += 1
        state["second"] = state["models"] == 2
        try:
            return consume(*args)
        finally:
            state["second"] = False
    def transaction(request, decide, **kwargs):
        selected = state["second"] and not state["controller"]
        if selected:
            state["attempt"] += 1
        return transact(request, decide, **kwargs)
    def send(request, **kwargs):
        if state["second"] and not state["controller"] and state["attempt"] and state["attempt"] not in injected and request.method == "GET":
            injected.add(state["attempt"])
            response = transport(request, **kwargs)
            state["controller"] = True
            try:
                if necessary:
                    controller_write(h, 2)
                drain(c, pending=opt_in)
            finally:
                state["controller"] = False
            return response
        response = transport(request, **kwargs)
        if request.method == "POST" and state["controller"]:
            ops, _ = parse_batch(request)
            receipt = next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
            value = receipt["result"]
            label = ("query" if "query_binding" in value else "probe" if "probe" in value else
                     "report" if "probe_id" in value else "observe" if "status" in value else
                     "scan" if "page" in value else "done" if "page_completed" in value else "other")
            commits.append(label)
        return response
    monkeypatch.setattr(c.authority, "consume_model", consume_model)
    monkeypatch.setattr(c.store, "transact", transaction)
    monkeypatch.setattr(c.transport, "send", send)
    h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    h.supervisor.join(timeout=70)
    result = h.supervisor.observe(c.launch_id)
    # #197: these controller writes land before the second model's ledger reads its keys, so the moved guard is
    # accepted and the commit re-based instead of exhausting three attempts. Which writers each mode needs is unchanged.
    assert result.phase == "sealed" and result.outcome == "succeeded"
    assert len(h.endpoint.requests) == 3
    assert len([r for r in c.requests if r.url.path.endswith("/model")]) == 3
    assert state["models"] == 3 and len(injected) == 1
    expected = (["query", "probe", "report", "observe"] if necessary else []) + ([] if opt_in else ["scan", "done"])
    assert commits == expected
    after = c.store.read_snapshot((budget_key, quota_key))
    debited = before[budget_key].payload["data"]["remaining_units"] - after[budget_key].payload["data"]["remaining_units"]
    assert debited == 5
    scans = before[quota_key].payload["data"]["scans_remaining"] - after[quota_key].payload["data"]["scans_remaining"]
    assert scans == (0 if opt_in else 1)
    assert len(h.supervisor.threads) == 1
    print(f"opt_in={opt_in}; necessary_query_report={necessary}; controller_guard_writes={len(commits)}; "
          f"new_scans={scans}; provider={len(h.endpoint.requests)}; units_debited={debited}")
