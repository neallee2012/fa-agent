"""Original second model, actual SDK and controller ledger; no provider replay."""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from shared_context.behavior_assistant import key
from shared_context.behavior_assistant import _Session, _row
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import worker_key
from shared_context.behavior_assistant_wire import TurnStopped
from shared_context.errors import Problem
from test_behavior_assistant_claim_boundaries import hosted_flow, reviewed, run
from test_behavior_assistant_acquire_conflicts import native_acquire, current_rotation
from test_behavior_rotation_cutoff import two_rotations
from test_behavior_rotation_settlement_history import current_cancelled
from test_behavior_runtime_rotation import rotated
from test_m3_auth import jwk_client, signing_key
from test_behavior_store import before_closing_guard, rewrite_key, stored_row


pytestmark = pytest.mark.parametrize("native_acquire", ["before-native"], indirect=True)


def independent_controller(callback, *args):
    # The real controller is not part of the model's pure read callback.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(callback, *args).result(timeout=20)


@pytest.mark.parametrize("phase", ["source-read", "ledger-read", "ledger-submit"])
def test_second_reserved_model_survives_one_true_noeffect_controller_conflict(hosted_flow, monkeypatch, phase):
    h, c = hosted_flow, hosted_flow.c
    budget_key = control_key("budget", c.binding.owner.root_budget_id)
    budget_before = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
    consume, source = c.authority.consume_model, c.authority._reviewed_once
    state = {"number": 0, "active": False, "source": False}
    seen = []
    def reviewed_once(*args):
        state["source"] = True
        try:
            return source(*args)
        finally:
            state["source"] = False
    def consume_model(*args):
        state["number"] += 1
        state["active"] = state["number"] == 2
        try:
            return consume(*args)
        finally:
            state["active"] = False
    monkeypatch.setattr(c.authority, "_reviewed_once", reviewed_once)
    monkeypatch.setattr(c.authority, "consume_model", consume_model)
    transport = c.transport.send
    def send(request, **kwargs):
        matches = (state["active"] and not seen and
            (phase == "source-read" and state["source"] and request.method == "GET"
             or phase == "ledger-read" and not state["source"] and request.method == "GET"
             or phase == "ledger-submit" and request.method == "POST"))
        response = None
        if matches:
            seen.append(phase)
            if request.method == "GET":
                response = transport(request, **kwargs)
            independent_controller(c.tick)
        return response if response is not None else transport(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    run(h)
    assert seen == [phase] and state["number"] == 3
    assert len([r for r in c.requests if r.url.path.endswith("/model")]) == 3
    assert len(h.endpoint.requests) == 3 and len(h.supervisor.threads) == 1
    assert c.store.read_snapshot((budget_key,))[budget_key].payload["data"]["remaining_units"] == budget_before["remaining_units"] - 5


def controller_write(h, number):
    c = h.c
    if number == 1:
        c.tick()
        return "delivery-drain"
    def query(binding):
        probe = c.native.probe(binding)
        launch = c.native.launch(c.launch_id)
        return h.host.observe(launch.native_invocation_id, c.identity.session_id, probe.probe_id).evidence_id
    workers = c.native.workers
    workers.adapter = replace(workers.adapter, query=query)
    workers.reconcile(c.binding.job_id, c.binding.owner)
    return "query-probe-report-observe"


@pytest.mark.parametrize("phase", ["source-read", "ledger-read", "ledger-submit"])
@pytest.mark.parametrize("conflicts", [2, 3])
def test_original_second_model_distinguishes_success_from_three_attempt_exhaustion(
        hosted_flow, monkeypatch, caplog, phase, conflicts):
    h, c = hosted_flow, hosted_flow.c
    budget_key = control_key("budget", c.binding.owner.root_budget_id)
    budget_before = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
    consume, reviewed_once, transact, transport = (
        c.authority.consume_model, c.authority._reviewed_once, c.store.transact, c.transport.send)
    state = {"number": 0, "active": False, "source": 0, "ledger": 0, "inside_controller": False}
    counts = {"source": 0, "ledger": 0}
    seen, writers = set(), []
    from threading import get_ident
    def read(*args):
        if state["active"]:
            counts["source"] += 1
            state["source"], state["owner"] = counts["source"], get_ident()
        try:
            return reviewed_once(*args)
        finally:
            state["source"] = 0
    def consume_model(*args):
        state["number"] += 1
        state["active"] = state["number"] == 2
        try:
            return consume(*args)
        finally:
            state["active"] = False
    def commit(request, decide, **kwargs):
        target = (state["active"] and not state["inside_controller"] and not state["source"])
        if target:
            counts["ledger"] += 1
            state["ledger"], state["owner"] = counts["ledger"], get_ident()
        try:
            return transact(request, decide, **kwargs)
        finally:
            if target:
                state["ledger"] = 0
    def controller(attempt):
        seen.add(attempt)
        state["inside_controller"] = True
        try:
            writers.append(independent_controller(controller_write, h, attempt))
        finally:
            state["inside_controller"] = False
    def conflict(keys):
        # Read phases: the controller write and a rewrite of one re-checked key land just before this
        # phase's own closing guard check (#197 accepts an unrelated writer alone).
        attempt = state["source"] if phase == "source-read" else state["ledger"]
        if (phase == "ledger-submit" or not state["active"] or state["inside_controller"]
                or state.get("owner") != get_ident() or phase == "ledger-read" and state["source"]
                or not attempt or attempt > conflicts or attempt in seen
                or stored_row(c.transport.backing, keys) is None):
            return
        controller(attempt)
        rewrite_key(c.transport.backing, keys)
    before_closing_guard(monkeypatch, c.store, conflict)
    def send(request, **kwargs):
        attempt = state["ledger"]
        if (phase == "ledger-submit" and state["active"] and not state["inside_controller"]
                and state.get("owner") == get_ident() and attempt and attempt <= conflicts
                and attempt not in seen and request.method == "POST"):
            controller(attempt)
        return transport(request, **kwargs)
    monkeypatch.setattr(c.authority, "consume_model", consume_model)
    monkeypatch.setattr(c.authority, "_reviewed_once", read)
    monkeypatch.setattr(c.store, "transact", commit)
    monkeypatch.setattr(c.transport, "send", send)
    if conflicts == 2:
        run(h)
        assert len(h.endpoint.requests) == 3
    else:
        assert h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)["status"] == "accepted"
        h.supervisor.join(timeout=70)
        observation = h.supervisor.observe(c.launch_id)
        assert observation.phase == "spawn_unknown" and observation.sdk_stop is None
        inventory = c.store.read_inventory()
        sid = key("session-id", c.starts[0].binding.start_request_id)
        session = _row(inventory.rows, key("session", sid), _Session)
        assert session.state == "waiting" and session.reason == "sdk_failed"
        assert session.model_requests == 2 and session.context_read
        intents = [row.payload["data"] for row_id, row in inventory.rows.items()
                   if isinstance(d := row.payload["data"].get("request_sha256"), str)
                   and row_id == key("model-intent", sid, d)]
        assert len(intents) == 2 and sorted(item["consumed"] for item in intents) == [False, True]
        assert key("artifact", c.binding.work.work_id) not in inventory.rows
        assert key("result", c.binding.work.work_id) not in inventory.rows
        with h.supervisor.journal.connect() as db:
            failure = json.loads(db.execute("SELECT body FROM assistant_failures WHERE launch_id=?",
                                           (c.launch_id,)).fetchone()[0])
        assert failure["upstream_status"] == 412 and failure["upstream_error_code"] == "BEHAVIOR_CONFLICT"
        assert failure["upstream_error_phase"] == "model_consume"
        assert len(h.endpoint.requests) == 1
        assert len([r for r in c.requests if r.url.path.endswith("/model")]) == 2
        assert h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)["status"] == "pending"
    assert seen == set(range(1, conflicts + 1))
    assert writers == ["delivery-drain", *(["query-probe-report-observe"] * (conflicts - 1))]
    assert counts["source" if phase == "source-read" else "ledger"] == 3
    prefix = "ASSISTANT_READ_CONFLICT phase=reviewed_sources" if phase == "source-read" else "ASSISTANT_LEDGER_CONFLICT"
    assert len([rec for rec in caplog.records if rec.getMessage().startswith(prefix)]) == 2
    assert len(h.supervisor.threads) == 1
    after = c.store.read_snapshot((budget_key,))[budget_key].payload["data"]
    assert after["remaining_units"] == budget_before["remaining_units"] - (5 if conflicts == 2 else 2)
    assert after["remaining_attempts"] == budget_before["remaining_attempts"] - (5 if conflicts == 2 else 2)
    print(f"second_model_phase={phase}; rejected_attempts={conflicts}; attempts=3; "
          f"HTTPmodel={len([r for r in c.requests if r.url.path.endswith('/model')])}; "
          f"provider={len(h.endpoint.requests)}")


@pytest.mark.parametrize("when", ["before-ledger", "after-ledger"])
def test_rejected_second_consume_rechecks_late_session_stop_before_marking_consumed(hosted_flow, monkeypatch, when):
    h, c = hosted_flow, hosted_flow.c
    consume, transport = c.authority.consume_model, c.transport.send
    counter, selected, changed = [], {}, []
    def consume_model(session, command, principal):
        counter.append(command.request_id)
        if len(counter) == 2:
            selected.update(session=session, digest=command.request_id)
        return consume(session, command, principal)
    monkeypatch.setattr(c.authority, "consume_model", consume_model)
    def send(request, **kwargs):
        target = selected.get("digest")
        if (target and not changed and request.method == "POST"
                and key("model-send", selected["session"].assignment.session_id, target).encode() in request.body):
            changed.append(True)
            if when == "after-ledger":
                response = transport(request, **kwargs)
            c.authority.stopped(selected["session"], TurnStopped(request_id="turn-stopped", reason="sdk_failed"))
            if when == "after-ledger":
                return response
        return transport(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    assert h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)["status"] == "accepted"
    h.supervisor.join(timeout=70)
    # A stop after consume's CAS commit is inside the seconds-level window: that one request is sent, never resent.
    assert changed == [True] and len(counter) == 2
    assert len(h.endpoint.requests) == (2 if when == "after-ledger" else 1)
    sid = selected["session"].assignment.session_id
    intent = c.store.read_snapshot((key("model-intent", sid, selected["digest"]),))
    assert next(iter(intent.values())).payload["data"]["consumed"] is (when == "after-ledger")
    assert h.supervisor.observe(c.launch_id).phase == "spawn_unknown"
    assert len([r for r in c.requests if r.url.path.endswith("/model")]) == 2


@pytest.mark.parametrize("change", ["ticket", "execution", "assignment", "binding"])
def test_second_consume_conflict_rechecks_exact_session_identity(hosted_flow, monkeypatch, change):
    from test_behavior_settlement import write
    h, c = hosted_flow, hosted_flow.c
    consume, transport = c.authority.consume_model, c.transport.send
    commands, selected, changed = [], {}, []
    def consume_model(session, command, principal):
        commands.append(command)
        if len(commands) == 2:
            selected.update(session=session, digest=command.request_id)
        return consume(session, command, principal)
    monkeypatch.setattr(c.authority, "consume_model", consume_model)
    def send(request, **kwargs):
        sid = selected["session"].assignment.session_id if selected else None
        if (sid and not changed and request.method == "POST"
                and key("model-send", sid, selected["digest"]).encode() in request.body):
            changed.append(True)
            session = selected["session"]
            if change == "ticket":
                updated = session.model_copy(update={"ticket_digest": "e" * 64})
            elif change == "execution":
                updated = session.model_copy(update={"execution": session.execution.model_copy(update={
                    "owner": session.execution.owner.model_copy(update={"epoch": session.execution.owner.epoch + 1})})})
            elif change == "assignment":
                updated = session.model_copy(update={"assignment": session.assignment.model_copy(update={
                    "task": session.assignment.task.model_copy(update={"operation_id": "different-original"})})})
            else:
                updated = session.model_copy(update={"binding_id": "another-assignment"})
            write(c.competitor, key("session", sid), updated.model_dump(mode="json"))
        return transport(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    h.supervisor.join(timeout=70)
    assert changed == [True] and len(h.endpoint.requests) == 1 and len(commands) == 2
    k = key("model-intent", selected["session"].assignment.session_id, selected["digest"])
    assert c.store.read_snapshot((k,))[k].payload["data"]["consumed"] is False
    assert h.supervisor.observe(c.launch_id).phase == "spawn_unknown"


@pytest.mark.parametrize("fault", ["before-commit", "after-commit", "fake-conflict"])
def test_unknown_second_model_outcome_is_never_a_noeffect_retry(hosted_flow, monkeypatch, fault):
    from azure.core.exceptions import ServiceRequestError
    h, c = hosted_flow, hosted_flow.c
    consume, transport = c.authority.consume_model, c.transport.send
    selected, commands, faults = {}, [], []
    def consume_model(session, command, principal):
        commands.append(command)
        if len(commands) == 2:
            selected.update(session=session, digest=command.request_id)
        return consume(session, command, principal)
    monkeypatch.setattr(c.authority, "consume_model", consume_model)
    def send(request, **kwargs):
        sid = selected["session"].assignment.session_id if selected else None
        if (sid and not faults and request.method == "POST"
                and key("model-send", sid, selected["digest"]).encode() in request.body):
            faults.append(True)
            if fault == "after-commit":
                transport(request, **kwargs)
            if fault == "fake-conflict":
                raise Problem(412, "NOT_BEHAVIOR_CONFLICT", "synthetic unknown rejection")
            raise ServiceRequestError("synthetic original response lost")
        return transport(request, **kwargs)
    monkeypatch.setattr(c.transport, "send", send)
    h.host.start(h.payload, "offline-full-invocation", c.identity.session_id)
    h.supervisor.join(timeout=70)
    assert faults == [True] and len(h.endpoint.requests) == 1
    assert len(commands) == 2 and len([r for r in c.requests if r.url.path.endswith("/model")]) == 2
    k = key("model-intent", selected["session"].assignment.session_id, selected["digest"])
    assert c.store.read_snapshot((k,))[k].payload["data"]["consumed"] is (fault == "after-commit")
    assert h.supervisor.observe(c.launch_id).phase == "spawn_unknown"
