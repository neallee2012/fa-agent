"""Row-scoped pure reads: unrelated writers may move the shared guard, read rows may not."""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shared_context import behavior_assistant
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit, Reservation
from shared_context.behavior_operations import control_key
from shared_context.behavior_store import GUARD_KEY, BehaviorStore
from shared_context.behavior_wire import Command
from shared_context.behavior_workers import ExecutorEvidence
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_behavior_store import FakeTable, SCOPE, changes, code, request, seed, store, write
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from work_policy_lifecycle_helpers import enabled
from session_authority_read_helpers import runtime_path


def _in_other_thread(action):
    def run():
        with ThreadPoolExecutor(max_workers=1) as writer:
            return writer.submit(action).result()
    return run


def _read_with_writer(adapter, keys, commit, *, row_scoped):
    def read():
        value = adapter.read_snapshot(keys)
        _in_other_thread(commit)()
        assert adapter.read_snapshot(keys) == value
        return value
    return adapter.consistent_read(read, row_scoped=row_scoped)


@pytest.mark.parametrize("row_scoped", [False, True])
def test_unrelated_writer_only_passes_row_scoped_read_after_exact_reverification(store, row_scoped):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    commit = lambda: other.transact(request("unrelated", ("owner",)), lambda _: changes(
        write("owner", {"epoch": 2}, "replace")))
    client.reads.clear()
    if not row_scoped:
        with pytest.raises(Problem) as failure:
            _read_with_writer(adapter, ("budget", "missing"), commit, row_scoped=False)
        assert code(failure) == "BEHAVIOR_CONFLICT"
        return
    result = _read_with_writer(adapter, ("budget", "missing"), commit, row_scoped=True)
    assert result["budget"].payload["data"]["remaining"] == 1 and result["missing"] is None
    assert sorted(key for _, key in client.reads) == sorted([
        GUARD_KEY, GUARD_KEY, "behavior:v1:row:budget", "behavior:v1:row:budget",
        "behavior:v1:row:missing", "behavior:v1:row:missing"])


@pytest.mark.parametrize("change", ["same-row", "missing-row-created", "receipt-created"])
def test_row_scoped_read_rejects_any_change_to_what_it_read(store, change):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    if change == "same-row":
        keys, commit = ("budget",), lambda: other.transact(request("spend", ("budget",)), lambda _: changes(
            write("budget", {"remaining": 0}, "replace")))
    elif change == "missing-row-created":
        keys, commit = ("missing",), lambda: other.transact(request("create"), lambda _: changes(
            write("missing", {"created": True})))
    else:
        keys, commit = (), lambda: other.transact(request("later"), lambda _: changes(write("later", {})))

    def read():
        value = adapter.read_snapshot(keys)
        if change == "receipt-created":
            with pytest.raises(Problem):
                adapter.reconcile(request("later"))
        _in_other_thread(commit)()
        return value
    with pytest.raises(Problem) as failure:
        adapter.consistent_read(read, row_scoped=True)
    assert code(failure) == "BEHAVIOR_CONFLICT"


def test_row_scoped_read_still_cannot_borrow_another_store_or_accept_nonboolean(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)
    with pytest.raises(Problem) as crossed:
        adapter.consistent_read(lambda: other.read_snapshot(("budget",)), row_scoped=True)
    assert code(crossed) == "BEHAVIOR_READ_SCOPE_INVALID"
    with pytest.raises(Problem) as flag:
        adapter.consistent_read(lambda: None, row_scoped=1)
    assert code(flag) == "BEHAVIOR_READ_SCOPE_INVALID"


@pytest.mark.parametrize("fault", ["epoch", "installation", "same-config-image", "guard-etag", "guard-missing"])
def test_row_scoped_migration_view_keeps_guard_epoch_and_installation_checks(fault):
    from shared_context.behavior_migration import MigrationStore
    client = FakeTable()
    adapter = MigrationStore(client, SCOPE)
    adapter.initialize()
    adapter.transact(request("install"), lambda _: changes(write("install", {"configuration_sha256": "a" * 64})))
    adapter.bind_installation("install", "a" * 64)

    def read():
        adapter.read_snapshot(("install",))
        k = GUARD_KEY if fault in {"epoch", "guard-etag", "guard-missing"} else "behavior:v1:row:install"
        entity = client.backing.rows[(SCOPE.table_name, SCOPE.partition_key, k)]
        if fault == "guard-missing":
            del client.backing.rows[(SCOPE.table_name, SCOPE.partition_key, k)]
        elif fault == "guard-etag":
            entity.metadata["etag"] = 'W/"replacement"'
        else:
            payload = json.loads(entity["Payload"])
            if fault == "epoch":
                payload.update(schema_version=2, configuration_sha256="b" * 64)
            elif fault == "installation":
                payload["data"]["configuration_sha256"] = "b" * 64
            else:
                payload["data"]["changed_metadata"] = True
            entity["Payload"] = json.dumps(payload).encode()
        return "not an authorized result"
    with pytest.raises(Problem):
        adapter.consistent_read(read, row_scoped=True)


def _fresh_turn_beside_uncertain_work(assistant):
    h, control, human, _ = enabled(assistant)
    uncertain, _ = control.submit(human, InstructionSubmit(request_id="old-uncertain", content="Old work."))
    fresh, _ = control.submit(human, InstructionSubmit(request_id="fresh-turn", content="Explain recursion.",
                                                       model="configured-second-model"))
    starts, evidences = [], {}
    h.workers.adapter = replace(h.workers.adapter, start=lambda start: starts.append(start))
    h.workers.start(uncertain.job_id, uncertain.owner)
    h.workers.start(fresh.job_id, fresh.owner)

    def query(binding):
        evidence = ExecutorEvidence(evidence_id=f"offline-unknown-{len(evidences) + 1}", binding=binding,
            sequence=len(evidences) + 1, native_execution_id=None, state="unknown", entire_tree_idle=False,
            all_inflight_starts_fenced=False, proof=b"OFFLINE observer: unknown; not a stop proof")
        evidences[evidence.evidence_id] = evidence
        return evidence.evidence_id

    h.workers.adapter = replace(h.workers.adapter, query=query, verify=lambda value: evidences[value])
    h.workers.reconcile(uncertain.job_id, uncertain.owner)
    assert h.workers.inspect(uncertain.job_id).state == "capacity_uncertain"
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    h.endpoint.expected_model = "configured-second-model"
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE response"}] * 2)
    provider = AssistantProvider(h.endpoint.expected_url, SimpleNamespace(
        get_token=lambda _: SimpleNamespace(token="OFFLINE-not-a-credential")), transport=h.endpoint.transport)
    h.client = TestClient(create_assistant_app(h.assistant, h.verifier, provider=provider))
    assignment, ticket = h.assistant.issue(next(item for item in starts if item.binding.job_id == fresh.job_id))
    root = f"/behavior/v1/bindings/{fresh.binding_id}/assistant/sessions/{assignment.session_id}/"
    headers = {**h.headers("agent"), "x-behavior-assistant-ticket": ticket.get_secret_value()}
    assert h.client.post(root + "acquire", content=Command(request_id="sdk-start").model_dump_json(),
                         headers=headers).status_code == 200
    return h, uncertain, fresh, root, headers, control, human


def _writer_inside_each_session_read(h, monkeypatch, writes, phase="session_authority"):
    """Another thread commits inside the chosen query view or after the single session read."""
    local, pending = threading.local(), list(writes)
    conflicts, consistent_read = behavior_assistant._read_conflicts, h.store.consistent_read
    read_record = h.store.read_record

    def selected(read, **kwargs):
        local.session = kwargs["phase"] == phase
        try:
            return conflicts(read, **kwargs)
        finally:
            local.session = False

    def raced(callback, **kwargs):
        if not getattr(local, "session", False) or not pending:
            return consistent_read(callback, **kwargs)

        def read():
            result = callback()
            writer = threading.Thread(target=pending.pop(0))
            writer.start()
            writer.join()
            return result
        return consistent_read(read, **kwargs)

    def raced_record(key):
        result = read_record(key)
        if phase == "session_authority" and pending:
            writer = threading.Thread(target=pending.pop(0))
            writer.start()
            writer.join()
        return result

    monkeypatch.setattr(behavior_assistant, "_read_conflicts", selected)
    monkeypatch.setattr(h.store, "consistent_read", raced)
    monkeypatch.setattr(h.store, "read_record", raced_record)
    return pending


def _reserve(h, root, headers, label):
    body = {"model": "configured-second-model", "stream": True, "messages": [{"role": "user", "content": label}]}
    request_hash = digest(body)
    return h.client.post(root + "reserve-model", content=Reservation(
        request_id=request_hash, request_sha256=request_hash).model_dump_json(), headers=headers)


def test_unrelated_observer_writes_during_session_read_do_not_refuse_reserve(assistant, monkeypatch, caplog):
    h, uncertain, _, root, headers, _, _ = _fresh_turn_beside_uncertain_work(assistant)
    ticks = []

    def tick():
        h.time += timedelta(seconds=1)
        ticks.append(h.runtime.controller.step(uncertain.binding_id).state)
    pending = _writer_inside_each_session_read(h, monkeypatch, [tick] * 3)
    guard = h.store._guard()[0].revision
    with caplog.at_level(logging.WARNING):
        reserved = _reserve(h, root, headers, "OFFLINE unrelated writers")
    assert reserved.status_code == 200, reserved.text
    assert ticks == ["capacity_uncertain"] and len(pending) == 2
    assert h.store._guard()[0].revision == guard + 3
    assert not [r for r in caplog.records if "READ_CONFLICT" in r.getMessage()]
    assert h.endpoint.requests == []


def test_revocation_inside_session_read_is_never_returned_as_current_authority(assistant, monkeypatch, caplog):
    h, _, fresh, root, headers, _, _ = _fresh_turn_beside_uncertain_work(assistant)
    owner = control_key("owner", fresh.owner.owner_record_id)
    _writer_inside_each_session_read(h, monkeypatch, [lambda: h.mutate(owner, active=False)])
    with caplog.at_level(logging.WARNING):
        refused = _reserve(h, root, headers, "OFFLINE revoked owner")
    assert refused.status_code == 403, refused.text
    # Ticket identity is cached; the original CAS gate still sees the changed owner in this first slice.
    assert [r.getMessage() for r in caplog.records if "READ_CONFLICT" in r.getMessage()] == []
    assert h.endpoint.requests == []


def test_standing_revocation_row_inserted_inside_session_read_still_refuses(assistant, monkeypatch, caplog):
    h, _, _, root, headers, control, human = _fresh_turn_beside_uncertain_work(assistant)
    assert h.store.read_snapshot((control.revocation_key,))[control.revocation_key] is None
    _writer_inside_each_session_read(h, monkeypatch, [lambda: control.revoke(human, "revoke-during-session-read")])
    with caplog.at_level(logging.WARNING):
        refused = _reserve(h, root, headers, "OFFLINE inserted revocation")
    assert refused.status_code == 403, refused.text
    assert h.store.read_snapshot((control.revocation_key,))[control.revocation_key] is not None
    # Refused by reserve-model's CAS gate, which reads the revocation row; the entry read does not.
    assert [r.getMessage() for r in caplog.records if "READ_CONFLICT" in r.getMessage()] == []
    assert h.endpoint.requests == []


def _tick(h, uncertain, ticks):
    def tick():
        h.time += timedelta(seconds=1)
        ticks.append(h.runtime.controller.step(uncertain.binding_id).state)
    return tick


@pytest.mark.parametrize("change", ["unrelated-observer", "inserted-revocation"])
def test_instruction_query_only_refuses_changes_to_what_it_read(assistant, monkeypatch, caplog, change):
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    h, uncertain, fresh, _, _, control, human = _fresh_turn_beside_uncertain_work(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier, installation=lambda: None))
    client = TestClient(h.app)
    conversation = control.records[fresh.binding_id].profile.instruction.conversation_id

    def observe():
        response = client.get("/work-policy/v1/conversations/" + conversation, headers=h.headers())
        return response.status_code, response.json().get("error", {}).get("code")

    ticks = []
    writer = (_tick(h, uncertain, ticks) if change == "unrelated-observer"
              else lambda: control.revoke(human, "revoke-during-read"))
    _writer_inside_each_session_read(h, monkeypatch, [writer] * 3, phase="instruction_query")
    with caplog.at_level(logging.WARNING):
        observed = observe()
    conflicts = [r.getMessage() for r in caplog.records if "READ_CONFLICT" in r.getMessage()]
    if change == "unrelated-observer":
        assert observed == (200, None) and conflicts == [] and ticks, (observed, conflicts)
    else:
        assert conflicts == ["ASSISTANT_READ_CONFLICT phase=instruction_query attempt=1 maximum=3"]
        assert h.store.read_snapshot((control.revocation_key,))[control.revocation_key] is not None
        assert observed == observe()
    assert h.endpoint.requests == []

def test_plain_snapshot_accepts_unrelated_guard_writer_only_after_rereading_its_keys(store):
    adapter, client = store
    seed(adapter)
    other = BehaviorStore(FakeTable(client.backing), SCOPE)

    writes = []

    def unrelated(key):
        if key == GUARD_KEY:
            client.after_get = unrelated
            return
        writes.append(key)
        other.transact(request(f"unrelated-{len(writes)}", ("owner",)), lambda _: changes(
            write("owner", {"epoch": len(writes) + 1}, "replace")))

    client.reads.clear()
    client.after_get = unrelated
    assert adapter.read_snapshot(("grant",))["grant"].payload["data"]["allowed"] is True
    assert [key for _, key in client.reads] == [GUARD_KEY, "behavior:v1:row:grant", GUARD_KEY, "behavior:v1:row:grant"]
    client.after_get = unrelated
    committed = adapter.transact(request("rebased-writer", ("grant",)), lambda _: changes(write("after", {})))
    assert committed.payload["guard_revision"] == adapter._guard()[0].revision


@pytest.mark.parametrize("revocation", ["owner", "standing-policy"])
def test_view_refresh_keeps_cache_but_revoked_binding_is_still_refused(assistant, revocation):
    h, _, fresh, root, headers, control, human = _fresh_turn_beside_uncertain_work(assistant)

    def read():
        control.refresh()
        control.refresh(fresh.binding_id)
        return fresh.binding_id in control.records

    assert h.store.consistent_read(read, max_keys=None)
    if revocation == "owner":
        h.mutate(control_key("owner", fresh.owner.owner_record_id), active=False)
    else:
        control.revoke(human, "revoke-after-view-refresh")
    assert h.store.consistent_read(read, max_keys=None)
    refused = _reserve(h, root, headers, "OFFLINE revoked after view refresh")
    assert refused.status_code == 403, refused.text
    assert h.endpoint.requests == []
