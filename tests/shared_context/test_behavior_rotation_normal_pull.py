"""Retirement after real current-policy pull producers, not retained settlement."""

from dataclasses import replace
from datetime import timedelta
import copy
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_delivery as delivery, behavior_wire as wire
from shared_context.behavior_migration import MigrationStore, installed_origin
from shared_context.behavior_runtime_rotation_history import validate_retirement
from shared_context.behavior_runtime_rotation import RotationHistory, rotation_key
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_runtime_transition import _payload_hash
from shared_context.behavior_server import build_server
from shared_context.errors import Problem
from shared_context.records import Record
from shared_context.source_materials import canonical
from test_behavior_rotation_cutoff import publish_inbox, publish_worker
from test_behavior_rotation_settlement_history import current_cancelled, next_rotation
from test_behavior_runtime_rotation import rotated
from test_behavior_runtime_rotation_sources import QueryTransport
from test_behavior_store import Credential, WireTransport
from test_m3_auth import jwk_client, signing_key


pytestmark = pytest.mark.parametrize("current_cancelled", [timedelta(seconds=1)], indirect=True)


@pytest.fixture(autouse=True)
def tokenizer(monkeypatch):
    import test_behavior_runtime_transition as transition
    original = transition.synthetic_configuration

    def configuration():
        config = original()
        return config.model_copy(update={"tokenizer": config.tokenizer.model_copy(
            update={"cache_directory": os.environ["TIKTOKEN_CACHE_DIR"]})})

    monkeypatch.setattr(transition, "synthetic_configuration", configuration)


@pytest.fixture
def normal_pull(current_cancelled, jwk_client):
    c = current_cancelled
    transport = QueryTransport()
    transport.backing = c.h.table.backing

    def forbidden(*args, **kwargs):
        pytest.fail("normal pull retirement must not contact external services")

    no_io = WireTransport()
    no_io.send = forbidden
    with build_server(
        c.config, credential=Credential(), table_transport=transport,
        source_transport=no_io, backup_transport=no_io, assistant_transport=httpx.MockTransport(forbidden),
        native_transports={target.executor_id: httpx.MockTransport(forbidden) for target in c.config.active_targets},
        key_client=jwk_client, clock=lambda: c.clock[0],
    ) as composition:
        runtime, binding = composition.runtime, c.config.bindings[0]
        installed_origin(runtime.store, c.config)
        _, worker = publish_worker(runtime.store, c.config, c.clock[0], "normal-pull-worker")
        _, _, inbox = publish_inbox(runtime.store, c.config, c.clock[0], worker, "normal-pull-inbox")
        runtime.dep.workers.adapter = replace(runtime.dep.workers.adapter, verify_readiness=lambda _: worker)
        runtime.dep.inbox.registration = replace(runtime.dep.inbox.registration, verify_deployment=lambda _: inbox)
        runtime.dep.delivery.registration = replace(
            runtime.dep.delivery.registration, verify_readiness=lambda: inbox.readiness,
        )
        delivery.DeliveryConsumer(runtime.dep.delivery).drain(
            binding.owner.root_budget_id, runtime.resolve_owner, c.config.controller_id, limit=1,
        )
        principal = replace(c.principal, actions=c.principal.actions | {"acknowledge"})
        received = runtime.fetch(binding.binding_id, wire.Command(request_id="normal-pull-fetch"), principal)
        acknowledged = runtime.acknowledge(
            binding.binding_id,
            wire.PullAcknowledgment(
                request_id="normal-pull-ack", exchange_id=received.exchange_id,
                acknowledgment_token=received.acknowledgment_token,
                binding=wire.DeliveryBinding.model_validate_json(received.binding.model_dump_json()),
            ),
            principal,
        )
        assert acknowledged.payload["result"]["fulfillment"] == "fulfilled"
        yield SimpleNamespace(case=c, runtime=runtime, table=composition.clients[0], transport=transport,
                              received=received, acknowledged=acknowledged)


def test_normal_pull_cancellation_remains_retirable_without_retained_authorization(normal_pull):
    h, c = normal_pull, normal_pull.case
    before = h.runtime.store.read_inventory()
    posts = sum(request.method == "POST" for request in h.transport.requests)
    assert installed_origin(MigrationStore(h.table, c.config.table.scope), c.config) == c.h.original
    _, liability = validate_retirement(before, c.config)
    assert liability["job_id"] == c.config.bindings[0].job_id
    assert liability["model_outcome"] == "not_requested"
    assert liability["model_request_sha256"] is None
    assert liability["final_delivery_id"] == h.received.binding.delivery_id
    assert h.runtime.store.read_inventory().entity_sha256 == before.entity_sha256
    assert sum(request.method == "POST" for request in h.transport.requests) == posts


@pytest.mark.parametrize("lost_response", [False, True])
def test_normal_pull_can_rotate_and_reread_after_completed_empty_scans(normal_pull, lost_response):
    h, c = normal_pull, normal_pull.case
    binding = c.config.bindings[0]
    for _ in range(2):
        delivery.DeliveryConsumer(h.runtime.dep.delivery).drain(
            binding.owner.root_budget_id, h.runtime.resolve_owner, c.config.controller_id, limit=1,
        )
    service = next_rotation(c)
    plan = service.plan(c.principal, rotation_id="normal-pull-next")
    authorization = service.authorize(
        c.principal, plan, decision_id="normal-pull-next-approved",
        expires_at=c.clock[0] + timedelta(minutes=5),
    )
    before = h.runtime.store.read_inventory()
    count = len(c.h.table.submissions)
    if lost_response:
        c.h.table.failure = "after"
        with pytest.raises(Problem):
            service.apply(c.principal, plan, authorization)
        c.h.table.failure = None
        service.reconcile(c.principal, plan, authorization)
    else:
        service.apply(c.principal, plan, authorization)
    assert len(c.h.table.submissions) == count + 1
    service.verify(c.principal, plan, authorization)
    fresh = MigrationStore(h.table, c.config.table.scope)
    assert installed_origin(fresh, service.new) == c.h.original
    after = fresh.read_inventory()
    current_key = rotation_key("current", c.config.table.scope.authority_domain)
    history = RotationHistory.model_validate_json(json.dumps(
        after.rows[rotation_key("history", plan.rotation_id)].payload["data"],
    ))
    assert history.previous_current.model_dump(mode="json") == before.rows[current_key].payload["data"]
    assert after.rows[current_key].payload["data"]["configuration_sha256"] == service.new.sha256
    changed = {current_key, *(image.row_id for image in plan.images)}
    assert all(after.rows[key] == row for key, row in before.rows.items() if key not in changed)
    assert all(after.receipts[key] == row for key, row in before.receipts.items())
    assert c.first_plan.liability.model_outcome == "unknown"
    assert c.first_plan.liability.model_request_sha256 == "e" * 64


def changed_inventory(inventory, *, row_changes=None, receipt_change=None, missing_receipt=None):
    rows, receipts = dict(inventory.rows), dict(inventory.receipts)
    hashes = dict(inventory.entity_sha256)
    for key, payload in (row_changes or {}).items():
        rows[key] = Record(payload, rows[key].etag)
        hashes["behavior:v1:row:" + key] = _payload_hash(payload)
        writers = [name for name, receipt in receipts.items() if key in dict(receipt.payload["writes"])]
        latest = max(writers, key=lambda name: receipts[name].payload["guard_revision"])
        value = copy.deepcopy(receipts[latest].payload)
        value["writes"] = [[row_id, _payload_hash(payload) if row_id == key else fingerprint]
                           for row_id, fingerprint in value["writes"]]
        receipts[latest] = Record(value, receipts[latest].etag)
        hashes["behavior:v1:receipt:" + latest] = _payload_hash(value)
    if receipt_change is not None:
        key, payload = receipt_change
        receipts[key] = Record(payload, receipts[key].etag)
        hashes["behavior:v1:receipt:" + key] = _payload_hash(payload)
    if missing_receipt is not None:
        receipts.pop(missing_receipt)
        hashes.pop("behavior:v1:receipt:" + missing_receipt)
    return replace(inventory, rows=rows, receipts=receipts, entity_sha256=hashes)


@pytest.mark.parametrize("mutation", [
    "missing-access", "missing-access-receipt", "access-read-set", "access-write-set",
    "fetch-read-set", "fetch-write-set", "fetch-image", "ack-write-set", "ack-image",
    "scope", "principal", "expiry", "proof-shape", "mixed-grant", "missing-material",
    "unknown-row", "extra-fetch",
])
def test_normal_pull_rejects_incomplete_or_rebound_history(normal_pull, mutation):
    h, c = normal_pull, normal_pull.case
    inventory = h.runtime.store.read_inventory()
    did = h.received.binding.delivery_id
    exkey = delivery.control_key("inbox-exchange", did)
    exchange = inventory.rows[exkey].payload["data"]
    access = exchange["authorization_evidence"]
    access_key = runtime_key("inbox-access", access["evidence_id"])
    access_request = runtime_key("evidence", "inbox-access", access["evidence_id"])
    fetch_request = exchange["fetch_request"]["request_id"]
    ack_request = delivery.control_key("inbox-acknowledge", did)
    if mutation == "missing-access":
        inventory = replace(inventory, rows={key: row for key, row in inventory.rows.items() if key != access_key})
    elif mutation == "missing-access-receipt":
        inventory = changed_inventory(inventory, missing_receipt=access_request)
    elif mutation in {"access-read-set", "access-write-set", "fetch-read-set", "fetch-write-set",
                      "fetch-image", "ack-write-set", "ack-image"}:
        request_id = access_request if mutation.startswith("access") else (
            fetch_request if mutation.startswith("fetch") else ack_request)
        payload = copy.deepcopy(inventory.receipts[request_id].payload)
        if mutation.endswith("read-set"):
            payload["request"]["read_set"] = payload["request"]["read_set"][:-1]
        elif mutation.endswith("write-set"):
            payload["writes"].append(["unrelated-normal-pull-write", "a" * 64])
        else:
            payload["writes"] = [[row_id, "a" * 64 if row_id == exkey else fingerprint]
                                 for row_id, fingerprint in payload["writes"]]
        inventory = changed_inventory(inventory, receipt_change=(request_id, payload))
    elif mutation in {"scope", "principal", "expiry", "proof-shape", "mixed-grant", "missing-material"}:
        from shared_context.behavior_inbox import VerifiedInboxAccess
        original = VerifiedInboxAccess.model_validate_json(canonical(access))
        origin = json.loads(original.proof)
        change = {}
        if mutation == "scope":
            change["binding"] = original.binding.model_copy(update={
                "route": original.binding.route.model_copy(update={"recipient_id": "another-recipient"}),
            })
        elif mutation == "principal":
            change["principal_id"] = "another-principal"
            origin["oid"] = "another-principal"
        elif mutation == "expiry":
            change["expires_at"] = original.expires_at + timedelta(minutes=1)
        elif mutation == "proof-shape":
            origin["request_digest"] = "not-a-digest"
        elif mutation == "mixed-grant":
            origin["authorization_id"] = "not-a-retained-grant"
        changed = original.model_copy(update={**change, "proof": canonical(origin)})
        access_payload = copy.deepcopy(inventory.rows[access_key].payload)
        access_payload["data"] = changed.model_dump(mode="json")
        access_payload["material"] = None if mutation == "missing-material" else delivery._material(
            changed.model_dump(mode="json")).model_dump(mode="json")
        exchange_payload = copy.deepcopy(inventory.rows[exkey].payload)
        exchange_payload["data"]["authorization_evidence"] = changed.model_dump(mode="json")
        exchange_payload["material"] = delivery._material(changed.model_dump(mode="json")).model_dump(mode="json")
        inventory = changed_inventory(inventory, row_changes={
            access_key: access_payload, exkey: exchange_payload,
        })
    elif mutation == "extra-fetch":
        payload = copy.deepcopy(inventory.rows[exkey].payload)
        payload["data"]["fetch_count"] = 2
        inventory = changed_inventory(inventory, row_changes={exkey: payload})
    else:
        key = "unclassified-normal-pull"
        inventory = replace(inventory, rows={**inventory.rows, key: Record(
            {"row_id": key, "binding": inventory.guard.payload["binding"], "data": {"unknown": True}}, "unknown",
        )})
    with pytest.raises(Problem):
        validate_retirement(inventory, c.config)
