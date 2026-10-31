"""Actual native-mode source producer and full model route read accounting."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, ContextVar
from datetime import timedelta
import json
from time import perf_counter
from urllib.parse import urlsplit

import pytest

from shared_context.behavior_assistant import _Session, _row, key
from shared_context.errors import Problem
from test_behavior_assistant_claim_boundaries import hosted_flow, reviewed
from test_behavior_assistant_acquire_conflicts import installed_assistant, native_acquire
from test_m3_auth import jwk_client, signing_key
from test_behavior_nonready_delivery import adequate_original_scans, drain
from test_behavior_second_model_conflicts import controller_write
from test_behavior_store import before_closing_guard, parse_batch, rewrite_key, stored_row


def test_true_reviewed_producer_reads_each_original_key_once(native_acquire, monkeypatch):
    c = native_acquire
    reviewed(c)
    c.hosted.acquire()
    sid = c.assignment.session_id
    session = _row(c.store.read_snapshot((key("session", sid),)), key("session", sid), _Session)
    send = c.transport.send
    calls = []
    def measured(request, **kwargs):
        calls.append(request.method)
        return send(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", measured)
    started = perf_counter()
    metrics, exact, snapshot = c.authority._reviewed_once(session, c.principal)
    assert metrics.item_count == 3 and metrics.completed_count == 2 and snapshot and exact.original
    print(f"source_point_GET={calls.count('GET')}; elapsed_ms={(perf_counter() - started) * 1000:.3f}")
    assert calls == ["GET"] * 17


@pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)
def test_full_model_keeps_both_inventories_and_original_egt(hosted_flow, monkeypatch):
    from shared_context.behavior_operations import control_key
    h, c = hosted_flow, hosted_flow.c
    phase = ContextVar("readview_measurement", default=None)
    send, handle = c.transport.send, c.api_transport.handle_request
    reviewed_once, evidence = c.authority._reviewed_once, c.composition.require_current_native_evidence
    counts, spans, models = Counter(), [], []
    budgetkey = control_key("budget", c.binding.owner.root_budget_id)
    before = c.store.read_snapshot((budgetkey,))[budgetkey].payload["data"]
    def model(request):
        if not request.url.path.endswith("/model"):
            return handle(request)
        models.append(True)
        token = phase.set("model" if len(models) == 2 else None)
        try:
            return handle(request)
        finally:
            phase.reset(token)
    def source(*args):
        if phase.get() is None:
            return reviewed_once(*args)
        token = phase.set("source")
        start = perf_counter()
        try:
            return reviewed_once(*args)
        finally:
            spans.append(round((perf_counter() - start) * 1000, 3))
            phase.reset(token)
    def full():
        if phase.get() is not None:
            counts["full_inventory_gate"] += 1
        return evidence()
    def measured(request, **kwargs):
        if phase.get() is not None:
            label = "EGT" if request.method == "POST" else (
                "inventory" if urlsplit(request.url).path.rstrip("()") == "/Behavior" else "point")
            counts[label] += 1
            if phase.get() == "source":
                counts["source_" + label] += 1
        return send(request, **kwargs)
    monkeypatch.setattr(c.api_transport, "handle_request", model)
    monkeypatch.setattr(c.authority, "_reviewed_once", source)
    monkeypatch.setattr(c.composition, "require_current_native_evidence", full)
    monkeypatch.setattr(c.transport, "send", measured)
    h.host.start(h.payload, "offline-readview-invocation", c.identity.session_id)
    h.supervisor.join(timeout=70)
    result = h.supervisor.observe(c.launch_id)
    assert result.phase == "sealed" and result.outcome == "succeeded"
    after = c.store.read_snapshot((budgetkey,))[budgetkey].payload["data"]
    assert before["remaining_units"] - after["remaining_units"] == 5
    assert len(models) == len(h.endpoint.requests) == 3 and len(h.supervisor.threads) == 1
    print(f"full_model={dict(counts)}; source_ms={spans}; provider=3; debits=5")
    assert counts == {"point": 72, "source_point": 9, "full_inventory_gate": 2, "EGT": 1}


@pytest.mark.usefixtures("adequate_original_scans")
@pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)
@pytest.mark.parametrize("enabled", [False, True], ids=["original", "guarded-view"])
@pytest.mark.parametrize("lane", ["source", "ledger", "egt"])
@pytest.mark.parametrize("conflicts,writer", [(1, "read-key"), (3, "read-key"), (1, "unrelated")],
                         ids=["1", "3", "1-unrelated"])
def test_real_controller_races_still_use_original_three_attempts(hosted_flow, monkeypatch, enabled, lane, conflicts,
                                                                  writer):
    h, c = hosted_flow, hosted_flow.c
    # #197: a read re-checks only its own keys, so only a write to one of them conflicts. The controller's
    # observation writes rows the model ledger reads, so only the reviewed-source read sees it as unrelated;
    # an EGT still conflicts on any write between decide and commit.
    retried = writer == "read-key" or lane != "source"
    conflicts = conflicts if retried else 1
    if not enabled:
        monkeypatch.setattr(c.store, "consistent_read", lambda callback, **_: callback())
    source, consume, transact, send = c.authority._reviewed_once, c.authority.consume_model, c.store.transact, c.transport.send
    from threading import get_ident
    state = {"models": 0, "active": False, "lane": None, "source": 0, "ledger": 0, "controller": False, "owner": None}
    injected, writes, timings = [], [], []
    def reviewed_probe(*args):
        if not state["active"] or state["controller"]:
            return source(*args)
        state["source"] += 1
        state["lane"], state["owner"] = "source", get_ident()
        started = perf_counter()
        try:
            return source(*args)
        finally:
            timings.append(("source", round((perf_counter() - started) * 1000, 3)))
            state["lane"] = None
    def model(*args):
        state["models"] += 1
        state["active"] = state["models"] == 2
        try:
            return consume(*args)
        finally:
            state["active"] = False
    def transaction(request, decide, **kwargs):
        active = state["active"] and not state["controller"]
        if active:
            state["ledger"] += 1
            state["lane"], state["owner"] = "ledger", get_ident()
        started = perf_counter()
        try:
            return transact(request, decide, **kwargs)
        finally:
            if active:
                timings.append(("ledger", round((perf_counter() - started) * 1000, 3)))
                state["lane"] = None
    def tick():
        state["controller"] = True
        try:
            controller_write(h, 2)
            assert drain(c) == ()
        finally:
            state["controller"] = False
    with ThreadPoolExecutor(max_workers=1) as executor:
        def conflict(keys):
            # Source/ledger lanes: a controller write lands just before this lane's own closing guard check.
            attempt = state["source" if lane == "source" else "ledger"]
            if (lane == "egt" or not state["active"] or state["controller"] or state["owner"] != get_ident()
                    or state["lane"] != ("source" if lane == "source" else "ledger")
                    or attempt in injected or attempt > conflicts
                    or writer == "read-key" and stored_row(c.transport.backing, keys) is None):
                return
            injected.append(attempt)
            executor.submit(Context().run, tick).result(timeout=20)
            if writer == "read-key":
                rewrite_key(c.transport.backing, keys)
        before_closing_guard(monkeypatch, c.store, conflict)
        def measured(request, **kwargs):
            attempt = state["ledger"]
            if (lane == "egt" and state["active"] and not state["controller"] and state["lane"] == "ledger"
                    and state["owner"] == get_ident() and attempt not in injected and attempt <= conflicts
                    and request.method == "POST"):
                injected.append(attempt)
                executor.submit(Context().run, tick).result(timeout=20)
            response = send(request, **kwargs)
            if state["controller"] and request.method == "POST":
                ops, _ = parse_batch(request)
                record = next(json.loads(op[1]["Payload"]) for op in ops if "behavior:v1:receipt:" in op[1]["RowKey"])
                value = record["result"]
                writes.append("query" if "query_binding" in value else "probe" if "probe" in value else
                              "report" if "probe_id" in value else "observe" if "status" in value else "unexpected")
            return response
        monkeypatch.setattr(c.authority, "_reviewed_once", reviewed_probe)
        monkeypatch.setattr(c.authority, "consume_model", model)
        monkeypatch.setattr(c.store, "transact", transaction)
        monkeypatch.setattr(c.transport, "send", measured)
        h.host.start(h.payload, "offline-race-invocation", c.identity.session_id)
        h.supervisor.join(timeout=70)
    outcome = h.supervisor.observe(c.launch_id)
    assert injected == list(range(1, conflicts + 1))
    assert writes == ["query", "probe", "report", "observe"] * conflicts
    assert state["source" if lane == "source" else "ledger"] == (1 if not retried else 2 if conflicts == 1 else 3)
    assert len(h.endpoint.requests) == (3 if conflicts == 1 else 1)
    assert outcome.phase == ("sealed" if conflicts == 1 else "spawn_unknown")
    if conflicts == 3:
        with h.supervisor.journal.connect() as db:
            failure = json.loads(db.execute("SELECT body FROM assistant_failures WHERE launch_id=?", (c.launch_id,)).fetchone()[0])
        assert failure["upstream_status"] == 412 and failure["upstream_error_phase"] == "model_consume"
    print(f"enabled={enabled}; lane={lane}; conflicts={conflicts}; writers={len(writes)}; "
          f"source_attempts={state['source']}; ledger_attempts={state['ledger']}; timings={timings}")


@pytest.mark.parametrize("fault", ["policy-clock", "readiness-clock", "cancel", "receipt", "material", "missing"])
def test_scope_never_caches_permission_or_hides_invalid_source(native_acquire, monkeypatch, fault):
    c = native_acquire
    reviewed(c)
    c.hosted.acquire()
    sid = c.assignment.session_id
    session = _row(c.store.read_snapshot((key("session", sid),)), key("session", sid), _Session)
    inventory = c.store.read_inventory()
    if fault in {"receipt", "material", "missing"}:
        _, _, snapshot = c.authority._reviewed_once(session, c.principal)
        source_id = next(k for k, row in snapshot.items() if row and "schema_id" in row.payload["data"])
        if fault == "receipt":
            selected = next(k for k, row in inventory.receipts.items() if source_id in dict(row.payload["writes"]))
            del c.transport.backing.rows[("Behavior", c.store._scope.partition_key, "behavior:v1:receipt:" + selected)]
        else:
            storage_key = ("Behavior", c.store._scope.partition_key, "behavior:v1:row:" + source_id)
            if fault == "missing":
                del c.transport.backing.rows[storage_key]
            else:
                entity = c.transport.backing.rows[storage_key]
                payload = json.loads(entity["Payload"])
                payload["material"]["sha256"] = "0" * 64
                entity["Payload"] = json.dumps(payload).encode()
        with pytest.raises(Problem):
            c.authority._reviewed_once(session, c.principal)
        return
    original = c.authority._reviewed_once
    calls = []
    def after_source(*args):
        result = original(*args)
        calls.append(True)
        if len(calls) == 1:
            if fault == "cancel":
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(c.native.workers.cancel, c.binding.job_id, c.binding.owner,
                                    expected_binding=c.starts[0].binding).result()
            else:
                when = (c.config.policies[0].policy.valid_until if fault == "policy-clock" else
                        c.h.harness.bundle.expires_at + timedelta(seconds=1))
                c.clock[0] = when
        return result
    monkeypatch.setattr(c.authority, "_reviewed_once", after_source)
    from shared_context.source_materials import digest
    # Sources are computed at reserve (not again after consume). The change lands right after that fresh
    # computation; neither reserve's CAS gate nor the model route may reuse it, and nothing reaches the provider.
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "synthetic"}],
            "stream": True, "max_tokens": 1024}
    import httpx
    entered = []
    c.composition.assistant.provider.transport = httpx.MockTransport(lambda _: entered.append(True))
    with pytest.raises(Problem):
        c.hosted.reserve_model(digest(body))
        c.hosted.model(httpx.Request("POST", "https://synthetic.invalid/model", json=body))
    assert calls and entered == []


def test_policy_time_is_revalidated_inside_the_same_view_without_guard_change(native_acquire, monkeypatch):
    c = native_acquire
    reviewed(c)
    c.hosted.acquire()
    sid = c.assignment.session_id
    session = _row(c.store.read_snapshot((key("session", sid),)), key("session", sid), _Session)
    guard = c.store._guard()
    context = c.authority.runtime.context
    calls = []
    def later(*args):
        calls.append(True)
        if len(calls) == 2:
            c.clock[0] = c.config.policies[0].policy.valid_until
        return context(*args)
    monkeypatch.setattr(c.authority.runtime, "context", later)
    with pytest.raises(Problem) as failure:
        c.authority._reviewed_once(session, c.principal)
    assert failure.value.body["error"]["code"] == "BEHAVIOR_CURRENT_POLICY_DENIED"
    assert len(calls) == 2 and c.store._guard() == guard
