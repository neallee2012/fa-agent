"""Captured guard1355 rows, real result/access/inbox code, offline Table transport."""
import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace, MappingProxyType

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_assistant import AssistantRuntime
from shared_context.behavior_delivery import (
    DeliveryCore, RuntimeRegistration, RuntimeContract, ChannelContract, DeliveryConsumer, _Obligation,
)
from shared_context.behavior_inbox import PullInbox, InboxRegistration
from shared_context.behavior_runtime import RuntimeBinding, RuntimeEvidenceAuthority, runtime_key
from shared_context.behavior_store import BehaviorStore, AuthorityScope, _Guard, GUARD_KEY
from shared_context.behavior_work_policy import WorkPolicyControl, StandingPolicyInstallation, WorkRegistration, WorkRegistry
from shared_context.behavior_workers import WorkerCore, WorkerContract
from shared_context.errors import Problem
from test_behavior_store import FakeTable, NativeEntity

# Needs the captured min-close-native-0921 artifacts outside the repo (see the Shared Context README); not a product check.
pytestmark = pytest.mark.skipif(not os.environ.get("ORIGINAL_RESULT_ART"), reason=(
    "needs ORIGINAL_RESULT_ART=<captured min-close-native-0921>\\contract "
    "(original-fetch-readonly.json sha256 0188f42f...; sibling parent-inputs\\normal-console-config.json)"))


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    import subprocess
    import requests
    import httpx
    def denied(*args, **kwargs):
        pytest.fail("continuation test must not launch a process or use external HTTP")
    monkeypatch.setattr(subprocess, "Popen", denied)
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.fixture
def captured():
    """Only the missing registry index is synthetic; all tested original rows retain exact bytes."""
    artifact = Path(os.environ["ORIGINAL_RESULT_ART"])
    raw = (artifact / "original-fetch-readonly.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "0188f42f8f6fec71605079db833a528fcda29f52bd4c05d96d1d092fa41bcd7c"
    doc = json.loads(raw)
    rows = {key: row for key, row in doc["rows"].items() if row is not None}
    binding = RuntimeBinding.model_validate_json(json.dumps(doc["binding"]))
    installed, = [StandingPolicyInstallation.model_validate_json(json.dumps(r["payload"]["data"]))
                  for r in rows.values() if r["payload"]["data"].get("schema_version") == "fa-steady-policy-installation-v1"]
    record, = [WorkRegistration.model_validate_json(json.dumps(r["payload"]["data"]))
               for r in rows.values() if r["payload"]["data"].get("kind") == "steady_work_registration"]
    control_binding = installed.policy.control_binding
    scope = AuthorityScope(authority_domain=installed.policy.authority_domain, table_name="Behavior", partition_key="OFFLINE")
    table = FakeTable()
    store = BehaviorStore(table, scope)
    # Offline import keeps captured payload/material and the installed policy bytes; only the store's
    # endpoint-derived envelope binding is pinned to the captured value as a local test seam.
    store._binding = control_binding
    for key, row in rows.items():
        payload = copy.deepcopy(row["payload"])
        payload["binding"] = control_binding
        entity = {"PartitionKey": scope.partition_key, "RowKey": "behavior:v1:row:" + key,
                  "Payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()}
        table.backing.rows[("Behavior", scope.partition_key, entity["RowKey"])] = NativeEntity(entity, row["etag"])
    store.initialize()
    now = datetime(2026, 9, 21, 10, 13, 44, tzinfo=timezone.utc)
    # Core contract only supplies constants; recorded digest checks use the real captured contract digest below.
    obrow = next(r["payload"]["data"] for r in rows.values() if r["payload"]["data"].get("kind") == "delivery_obligation")
    contract = RuntimeContract(team_id=binding.team_id, entry_id="OFFLINE",
        control_binding=control_binding, protection_version="inline-no-gc-v1",
        max_queue_seconds=installed.policy.queue_seconds,
        verifier_version=next(r["payload"]["data"]["verifier_version"] for r in rows.values()
                              if "definition_digest" in r["payload"]["data"] and "passed_checks" in r["payload"]["data"]),
        consumer_version="OFFLINE", timer_version="OFFLINE",
        channel=ChannelContract(channel_id="authenticated-pull", version="fa-pull-inbox-v1",
                                supported_levels=("client_received",), max_confirmation_level="client_received"))
    evidence = RuntimeEvidenceAuthority(store, clock=lambda: now, authentication_version=next(
        r["payload"]["data"]["authentication_version"] for r in rows.values()
        if r["payload"]["data"].get("schema_version") == "fa-current-work-result-access-v1"))
    inbox = PullInbox(store, InboxRegistration(evidence.authentication_version, evidence.verify_access, lambda _: None))
    core = DeliveryCore(store, clock=lambda: now, registration=RuntimeRegistration(contract, "native", channel_adapter=inbox))
    inbox.attach(core)
    workerrow = next(r["payload"]["data"] for r in rows.values() if r["payload"]["data"].get("kind") == "worker_job")
    worker = SimpleNamespace(configuration_digest=lambda: workerrow["configuration_digest"])
    # Construction-only adapter for omitted unrelated services. No permission or payload verifier is mocked.
    runtime = object.__new__(AssistantRuntime)
    runtime.store, runtime.mode, runtime.clock = store, "native", lambda: now
    runtime.bindings = MappingProxyType({installed.policy.template.binding_id: installed.policy.template})
    runtime.profiles = MappingProxyType({installed.policy.profile.binding_id: installed.policy.profile})
    runtime.teams_metrics, runtime.work_policy = None, None
    runtime.historical_binding_ids = frozenset()
    runtime.dep = SimpleNamespace(store=store, delivery=core, inbox=inbox, workers=worker,
                                 evidence=evidence, native_launches=None, source_backups=None, memory_projections=None)
    # Registered work is bound to the installed policy's exact sha256, so the policy itself stays unmodified.
    policy = installed.policy
    assert policy.control_binding == store._binding
    control = WorkPolicyControl(runtime, policy, lambda: pytest.fail("result read must not ask for cost"))
    registry = WorkRegistry(policy_id=policy.policy_id, entries=(record.admission_id,))
    from shared_context.behavior_store import ControlRequest, ControlChanges, ControlWrite
    store.transact(ControlRequest(request_id="OFFLINE-registry", request_digest="f" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=control.registry_key, mode="create",
            data=registry.model_dump(mode="json")),), result={}))
    runtime.work_policy = control
    inbox.work_policy, core.work_policy = control, control
    control.refresh()
    # Captured native digest is independent of the offline transport binding.
    core.configuration_digest = lambda: obrow["configuration_digest"]
    principal = BehaviorPrincipal(binding.tenant_id, binding.human_oid, binding.team_id,
                                  "human", None, frozenset({"read", "approve", "acknowledge"}))
    # A captured projection omits historic receipts. One explicitly synthetic
    # fixture import receipt anchors those images for the narrow new-history validator.
    from shared_context.behavior_store import _Receipt
    imports = []
    for (_, _, rowkey), entity in table.backing.rows.items():
        if rowkey.startswith("behavior:v1:row:"):
            imports.append((rowkey.removeprefix("behavior:v1:row:"), hashlib.sha256(entity["Payload"]).hexdigest()))
    receipt = _Receipt(binding=store._binding,
        request=ControlRequest(request_id="OFFLINE-captured-import", request_digest="a" * 64),
        writes=tuple(imports), result={}, guard_revision=1)
    entity = {"PartitionKey": scope.partition_key, "RowKey": "behavior:v1:receipt:OFFLINE-captured-import",
              "Payload": receipt.model_dump_json().encode()}
    table.backing.rows[("Behavior", scope.partition_key, entity["RowKey"])] = NativeEntity(entity, 'W/"offline-import"')
    return SimpleNamespace(runtime=runtime, control=control, store=store, table=table, inbox=inbox, core=core,
        binding=binding, principal=principal, command=wire.Command(request_id=doc["original_fetch_request_id"]),
        original=doc, before=copy.deepcopy(table.backing.rows))


def old_binding(h):
    from shared_context.behavior_delivery import _Outbox, _parse
    from shared_context.behavior_operations import control_key
    ob = next(r["payload"]["data"] for r in h.original["rows"].values()
              if r and r["payload"]["data"].get("kind") == "delivery_obligation")
    key = control_key("delivery-outbox", ob["final_delivery_id"])
    return key, _parse(h.store.read_snapshot((key,))[key].payload["data"], _Outbox).binding


def check_history(h):
    from shared_context.behavior_migration import AuthorityInventory
    from shared_context.behavior_result_delivery import validate_result_history
    from shared_context.records import Record
    rows, receipts, hashes = {}, {}, {}
    guard = None
    for (_, _, key), value in h.table.backing.rows.items():
        raw = value["Payload"]
        payload = json.loads(raw)
        saved = Record(payload, value.metadata["etag"])
        hashes[key] = hashlib.sha256(raw).hexdigest()
        if key == GUARD_KEY:
            guard = saved
        elif key.startswith("behavior:v1:row:"):
            rows[key.removeprefix("behavior:v1:row:")] = saved
        elif key.startswith("behavior:v1:receipt:"):
            receipts[key.removeprefix("behavior:v1:receipt:")] = saved
    inventory = AuthorityInventory(h.store._scope, guard, rows, receipts, hashes)
    config = SimpleNamespace(table=SimpleNamespace(scope=h.store._scope), sha256="f" * 64,
        workers=SimpleNamespace(control_binding=h.store._binding), delivery=h.core.contract)
    return validate_result_history(inventory, config, (h.binding,))


def test_captured_saved_cancelled_result_must_be_readable_without_accepting_queued_notice(captured):
    from shared_context.behavior_result_delivery import append_saved_result
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_delivery import _Outbox, _parse
    h = captured
    with pytest.raises(Problem, match="exact retained payload/route differs"):
        h.runtime.fetch(h.binding.binding_id, h.command, h.principal)
    obligation = next(r["payload"]["data"] for r in h.original["rows"].values()
                      if r and r["payload"]["data"].get("kind") == "delivery_obligation")
    oldkey = control_key("delivery-outbox", obligation["final_delivery_id"])
    old = h.store.read_snapshot((oldkey,))[oldkey]
    out = _parse(old.payload["data"], _Outbox)
    assert out.binding.version == 1 and old.payload["material"]["sha256"] == (
        "f690b2db23b94598d7d9d45fbf99a47b31c5945b2fb060fb5a9d1ac7c27fdab3")
    repair = append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, out.binding)
    assert repair.payload["result"]["version"] == 2
    assert h.store.read_snapshot((oldkey,))[oldkey] == old
    assert check_history(h)
    before_missing = copy.deepcopy(h.table.backing.rows)
    with pytest.raises(Problem) as missing:
        h.runtime.reconcile_fetch(h.binding.binding_id, h.command, h.principal)
    assert missing.value.body["error"]["code"] == "INBOX_FETCH_NOT_STARTED"
    assert h.table.backing.rows == before_missing
    with pytest.raises(Problem) as ordinary_retry:
        h.runtime.fetch(h.binding.binding_id, h.command, h.principal)
    assert ordinary_retry.value.body["error"]["code"] == "ORIGINAL_FETCH_CONTINUATION_REQUIRED"
    got = h.runtime.continue_fetch(h.binding.binding_id, h.command, h.principal)
    assert json.loads(got.payload)["work_state"] == "cancelled"
    assert got.binding.version == 2 and got.send_outcome == "unknown"
    assert h.runtime.reconcile_fetch(h.binding.binding_id, h.command, h.principal) == got
    assert check_history(h)
    before_replay = copy.deepcopy(h.table.backing.rows)
    assert h.runtime.continue_fetch(h.binding.binding_id, h.command, h.principal) == got
    assert append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, out.binding) == repair
    assert h.table.backing.rows == before_replay
    # A new record cannot enter current history with missing immutable material.
    from shared_context.behavior_result_delivery import continuation_key
    row_id = "behavior:v1:row:" + continuation_key(h.binding.binding_id)
    entity = h.table.backing.rows[("Behavior", h.store._scope.partition_key, row_id)]
    saved_bytes = entity["Payload"]
    altered = json.loads(saved_bytes)
    altered["material"] = None
    entity["Payload"] = json.dumps(altered, separators=(",", ":")).encode()
    with pytest.raises(Problem):
        check_history(h)
    entity["Payload"] = saved_bytes
    with pytest.raises(Problem):
        h.runtime.continue_fetch(h.binding.binding_id, wire.Command(request_id="another-fetch"), h.principal)
    # Every captured original except the obligation remains byte-for-byte
    # unchanged in this offline authority, including old access/outbox/protected data.
    for key, original in h.before.items():
        row_id = key[-1]
        if row_id != GUARD_KEY and not row_id.endswith(control_key("delivery-obligation", h.binding.obligation_id)):
            assert h.table.backing.rows[key] == original
    print("captured queued failure -> appended cancelled v2; old504B/access preserved; same fetchID; one exchange; pure reconcile")


def test_wrong_original_and_existing_exchange_refuse_without_writes(captured):
    from shared_context.behavior_result_delivery import append_saved_result
    from shared_context.behavior_operations import control_key
    from shared_context.behavior_store import ControlRequest, ControlChanges, ControlWrite
    h = captured
    _, old = old_binding(h)
    before = copy.deepcopy(h.table.backing.rows)
    for invalid in (old.model_copy(update={"payload_digest": "f" * 64}),
                    old.model_copy(update={"route": old.route.model_copy(update={"recipient_id": "wrong-human"})})):
        with pytest.raises(Problem):
            append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, invalid)
        assert h.table.backing.rows == before
    exchange = control_key("inbox-exchange", old.delivery_id)
    h.store.transact(ControlRequest(request_id="OFFLINE-foreign-exchange", request_digest="c" * 64),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=exchange, mode="create", data={"unknown": True}),), result={}))
    before = copy.deepcopy(h.table.backing.rows)
    with pytest.raises(Problem, match="original exchange exists"):
        append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, old)
    assert h.table.backing.rows == before


def test_cas_and_lost_append_or_fetch_response_use_same_receipt_without_resending(captured, monkeypatch):
    from shared_context.behavior_result_delivery import append_saved_result
    from test_behavior_store import conflict
    h = captured
    _, old = old_binding(h)
    before = copy.deepcopy(h.table.backing.rows)
    h.table.before_submit = lambda: (_ for _ in ()).throw(conflict(412))
    with pytest.raises(Problem) as denied:
        append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, old)
    assert denied.value.body["error"]["code"] == "BEHAVIOR_CONFLICT"
    assert h.table.backing.rows == before
    h.table.failure = "after"
    with pytest.raises(Problem) as lost:
        append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, old)
    assert lost.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    count = len(h.table.submissions)
    receipt = append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, old, reconcile=True)
    assert receipt.payload["result"]["version"] == 2 and len(h.table.submissions) == count
    h.table.failure = None
    # Lose the actual exchange EGT response, not the access creation before it.
    original_submit = h.table.submit_transaction
    def lose_exchange(operations, **kwargs):
        answer = original_submit(operations, **kwargs)
        if any(json.loads(op[1]["Payload"]).get("data", {}).get("kind") == "inbox_exchange" for op in operations):
            from azure.core.exceptions import ServiceRequestError
            raise ServiceRequestError("OFFLINE exchange ACK lost")
        return answer
    monkeypatch.setattr(h.table, "submit_transaction", lose_exchange)
    with pytest.raises(Problem) as unknown:
        h.runtime.continue_fetch(h.binding.binding_id, h.command, h.principal)
    assert unknown.value.body["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    count = len(h.table.submissions)
    result = h.runtime.reconcile_fetch(h.binding.binding_id, h.command, h.principal)
    assert json.loads(result.payload)["work_state"] == "cancelled"
    assert len(h.table.submissions) == count
    assert check_history(h)


def test_http_original_id_continue_reconcile_and_explicit_ack_use_exact_v2_binding(captured, monkeypatch):
    import base64
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from shared_context.auth import AuthSettings, Actor
    from shared_context.behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
    from shared_context.behavior_api import behavior_router, _install_error_handlers
    from shared_context.behavior_result_delivery import append_saved_result
    from shared_context.behavior_operations import control_key
    h = captured
    _, previous = old_binding(h)
    # The supplied projection excludes quota/index rows; add only those offline
    # ACK prerequisites using the existing Table producer, not invented native evidence.
    from shared_context.behavior_delivery import QuotaRecord, PendingIndexRecord, PendingEntry
    from shared_context.behavior_store import ControlRequest, ControlChanges
    obkey = control_key("delivery-obligation", h.binding.obligation_id)
    ob = _Obligation.model_validate_json(json.dumps(h.store.read_snapshot((obkey,))[obkey].payload["data"]))
    quota_writes = []
    for kind, scope_id in (("team", h.core.contract.team_id), ("entry", h.core.contract.entry_id),
                           ("root", h.binding.owner.root_budget_id)):
        quota_writes.append(h.core._write(control_key("delivery-quota", kind, scope_id), QuotaRecord(
            configuration_digest=h.core.configuration_digest(), scope_kind=kind, scope_id=scope_id,
            pending_limit=32, pending=1, root_request_id=ob.root_request_id if kind == "root" else None), "create"))
    quota_writes.append(h.core._write(control_key("delivery-index", h.binding.owner.root_budget_id),
        PendingIndexRecord(configuration_digest=h.core.configuration_digest(), root_budget_id=h.binding.owner.root_budget_id,
            entries=(PendingEntry(obligation_id=ob.obligation_id, owner_record_id=ob.owner_record_id,
                conversation_id=ob.conversation_id, review_at=ob.review_at, generation=ob.generation,
                automation=ob.automation, due_pending=ob.due_pending),)), "create"))
    h.store.transact(ControlRequest(request_id="OFFLINE-ACK-prerequisites", request_digest="d" * 64),
        lambda _: ControlChanges(writes=tuple(quota_writes), result={}))
    append_saved_result(h.runtime, h.binding.binding_id, h.command, h.principal, previous)
    auth = AuthSettings(h.binding.tenant_id, "api://33333333-3333-3333-3333-333333333333", (f"https://sts.windows.net/{h.binding.tenant_id}/",),
        {h.binding.agent_oid: Actor(h.binding.agent_oid, "offline-agent", h.binding.team_id, "offline")})
    client_id = "11111111-1111-1111-1111-111111111111"
    verifier = BehaviorJwtVerifier(auth, BehaviorAuthSettings(h.binding.human_oid, client_id, h.binding.team_id),
                                   key_client=SimpleNamespace())
    monkeypatch.setattr(verifier.tokens, "verified_claims", lambda _: {
        "oid": h.binding.human_oid, "iss": auth.issuers[0], "scp": "Behavior.Read", "appid": client_id,
    })
    app = FastAPI()
    _install_error_handlers(app)
    app.include_router(behavior_router(h.runtime, verifier))
    from starlette.requests import Request
    for suffix in ("pull-continue", "pull-reconcile"):
        route = next(route for route in app.routes if getattr(route, "path", "").endswith("/" + suffix))
        request = Request({"type": "http", "method": "POST", "path": "/unused", "headers": [],
                           "route": route, "path_params": {"binding_id": h.binding.binding_id}})
        assert h.control.result_route(request)
    base = "/behavior/v1/bindings/" + h.binding.binding_id + "/delivery"
    headers = {"Authorization": "Bearer OFFLINE", "Content-Type": "application/json"}
    with TestClient(app) as client:
        frozen = copy.deepcopy(h.table.backing.rows)
        missing = client.post(base + "/pull-reconcile", json=h.command.model_dump(mode="json"), headers=headers)
        assert missing.status_code == 409 and missing.json()["error"]["code"] == "INBOX_FETCH_NOT_STARTED"
        assert h.table.backing.rows == frozen
        assert client.post(base + "/pull-continue", json={"request_id": "different"}, headers=headers).status_code == 409
        assert client.post(base + "/pull-continue", json=h.command.model_dump(mode="json")).status_code == 401
        response = client.post(base + "/pull-continue", json=h.command.model_dump(mode="json"), headers=headers)
        assert response.status_code == 200, response.text
        assert json.loads(response.content)["work_state"] == "cancelled"
        assert response.headers["X-Behavior-Payload-SHA256"] == hashlib.sha256(response.content).hexdigest()
        binding = wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(response.headers["X-Behavior-Binding"]))
        assert binding.version == 2 and binding.obligation_id == previous.obligation_id
        replay_before = copy.deepcopy(h.table.backing.rows)
        reconciled = client.post(base + "/pull-reconcile", json=h.command.model_dump(mode="json"), headers=headers)
        assert reconciled.status_code == 200 and reconciled.content == response.content
        for key in ("X-Behavior-Exchange", "X-Behavior-Ack-Token", "X-Behavior-Binding"):
            assert reconciled.headers[key] == response.headers[key]
        assert h.table.backing.rows == replay_before
        obligation = control_key("delivery-obligation", h.binding.obligation_id)
        assert h.store.read_snapshot((obligation,))[obligation].payload["data"]["fulfillment"] == "pending"
        command = wire.PullAcknowledgment(request_id="OFFLINE-explicit-ACK",
            exchange_id=response.headers["X-Behavior-Exchange"], acknowledgment_token=response.headers["X-Behavior-Ack-Token"],
            binding=binding)
        bad = command.model_copy(update={"binding": wire.DeliveryBinding.model_validate_json(previous.model_dump_json())})
        assert client.post(base + "/acknowledge", content=bad.model_dump_json(), headers=headers).status_code == 403
        accepted = client.post(base + "/acknowledge", content=command.model_dump_json(), headers=headers)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["result"]["fulfillment"] == "fulfilled"
        assert check_history(h)
