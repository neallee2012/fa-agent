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
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import settle
from test_user_turn_read import user_history
from test_work_policy_lifecycle import enabled


@pytest.fixture
def runtime_path():
    return Path(__file__)


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


def _race_every_pure_read(h, monkeypatch, writer):
    """Commit from another thread between each read's rows and its final guard read.

    This includes the predicate reads inside transact; a decision and its EGT are not raced.
    """
    local = threading.local()
    physical, transact = h.store._read_physical, h.store.transact
    fired = []

    def racing(key, model):
        if not threading.current_thread().name.startswith("race-writer") and not getattr(local, "deciding", False):
            if key != GUARD_KEY:
                local.rows = True
            elif getattr(local, "rows", False):
                local.rows = False
                committed = threading.Thread(target=writer, name=f"race-writer-{len(fired)}")
                committed.start()
                committed.join()
                fired.append(key)
        return physical(key, model)

    def transacting(request, decide, **kwargs):
        def deciding(rows):
            local.deciding = True
            try:
                return decide(rows)
            finally:
                local.deciding = False
        return transact(request, deciding, **kwargs)

    monkeypatch.setattr(h.store, "_read_physical", racing)
    monkeypatch.setattr(h.store, "transact", transacting)
    return fired


@pytest.mark.parametrize("change", ["unrelated-writer", "owner-revoked"])
def test_model_authority_reads_only_refuse_changes_to_what_they_read(user_history, monkeypatch, change):
    import hashlib
    import httpx
    from shared_context.behavior_assistant import _LocalHttp
    from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
    from shared_context.behavior_work_policy_api import install_work_policy_routes
    c = user_history
    h = c.h
    h.endpoint.expected_url = "https://provider.invalid/openai/v1/chat/completions"
    h.endpoint.expected_model = "gpt-4.1-mini"
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "OFFLINE response"}] * 2)
    provider = AssistantProvider(h.endpoint.expected_url, SimpleNamespace(
        get_token=lambda _: SimpleNamespace(token="OFFLINE-not-a-credential")), transport=h.endpoint.transport)
    h.app = create_assistant_app(h.assistant, h.verifier, provider=provider)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    c.second.client.http._transport = _LocalHttp(h.client)
    body = {"model": "gpt-4.1-mini", "stream": True,
            "messages": [{"role": "user", "content": "OFFLINE model authority under unrelated writers"}]}
    request_hash = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                             ensure_ascii=False).encode()).hexdigest()
    owner = control_key("owner", c.second.binding.owner.owner_record_id)
    written = []

    def unrelated():
        row = control_key("unrelated-writer", str(len(written)))
        written.append(row)
        h.store.transact(ControlRequest(request_id=row, request_digest=hashlib.sha256(row.encode()).hexdigest(),
            read_set=(row,)), lambda _: ControlChanges(writes=(ControlWrite(row_id=row, mode="create", data={}),),
            result={"unrelated": True}))

    fired = _race_every_pure_read(h, monkeypatch, unrelated if change == "unrelated-writer"
                                  else lambda: written or (written.append(owner), h.mutate(owner, active=False)))
    if change == "unrelated-writer":
        assert c.second.client.read_user_turn(1).text
        c.second.client.reserve_model(request_hash)
        response = c.second.client.model(httpx.Request(
            "POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert response.status_code == 200 and fired and len(h.endpoint.requests) == 1
        settle(c.second, c.state)
        path = "/behavior/v1/bindings/" + c.second.binding.binding_id
        pulled = h.client.post(path + "/delivery/pull", content=Command(request_id="pull-under-writers")
                               .model_dump_json(), headers=h.headers())
        assert pulled.status_code == 200, pulled.text
        from shared_context import behavior_wire as wire
        import base64
        delivery = wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pulled.headers["x-behavior-binding"]))
        acked = h.client.post(path + "/delivery/acknowledge", content=wire.PullAcknowledgment(
            request_id="ack-under-writers", exchange_id=pulled.headers["x-behavior-exchange"],
            acknowledgment_token=pulled.headers["x-behavior-ack-token"], binding=delivery).model_dump_json(),
            headers=h.headers())
        assert acked.status_code == 200 and acked.json()["result"]["fulfillment"] == "fulfilled", acked.text
    else:
        with pytest.raises(Problem):
            c.second.client.reserve_model(request_hash)
            c.second.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert written == [owner] and h.endpoint.requests == []

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

def test_result_pull_decision_does_not_scan_other_registered_work(user_history, monkeypatch):
    """The fetch EGT decision re-checks only its reader; a scan of every registration widened the
    write window so any other turn's write refused the pull (live 412 before the ACK challenge)."""
    import base64
    from shared_context import behavior_wire as wire
    from shared_context.behavior_store import ControlChanges, ControlRequest, ControlWrite
    from shared_context.behavior_work_policy import work_policy_key
    from test_instruction_prompt import submit
    c = user_history
    h, control = c.h, c.control
    settle(c.second, c.state)
    for number in range(3):
        submit(h, f"other-registered-{number}", "Other registered work.")
    own = work_policy_key("registration", control.records[c.second.binding.binding_id].admission_id)
    others = {"behavior:v1:row:" + work_policy_key("registration", record.admission_id)
              for record in control.records.values()} - {"behavior:v1:row:" + own}
    local, physical, transact, fired = threading.local(), h.store._read_physical, h.store.transact, []

    def unrelated():
        row = control_key("unrelated-during-fetch", str(len(fired)))
        h.store.transact(ControlRequest(request_id=row, request_digest="c" * 64, read_set=(row,)),
                         lambda _: ControlChanges(writes=(ControlWrite(row_id=row, mode="create", data={}),),
                                                  result={"unrelated": True}))

    def racing(key, model):
        if getattr(local, "fetching", False) and key in others and not fired:
            fired.append(key)
            writer = threading.Thread(target=unrelated)
            writer.start()
            writer.join()
        return physical(key, model)

    def transacting(request, decide, **kwargs):
        if not decide.__qualname__.endswith("_fetch.<locals>.decide"):
            return transact(request, decide, **kwargs)

        def deciding(rows):
            local.fetching = True
            try:
                return decide(rows)
            finally:
                local.fetching = False
        return transact(request, deciding, **kwargs)

    monkeypatch.setattr(h.store, "_read_physical", racing)
    monkeypatch.setattr(h.store, "transact", transacting)
    path = "/behavior/v1/bindings/" + c.second.binding.binding_id
    pulled = h.client.post(path + "/delivery/pull", content=Command(request_id="pull-without-scan")
                           .model_dump_json(), headers=h.headers())
    assert pulled.status_code == 200 and fired == [], (pulled.text, fired)
    delivery = wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(pulled.headers["x-behavior-binding"]))
    acked = h.client.post(path + "/delivery/acknowledge", content=wire.PullAcknowledgment(
        request_id="ack-without-scan", exchange_id=pulled.headers["x-behavior-exchange"],
        acknowledgment_token=pulled.headers["x-behavior-ack-token"], binding=delivery).model_dump_json(),
        headers=h.headers())
    assert acked.status_code == 200 and acked.json()["result"]["fulfillment"] == "fulfilled", acked.text