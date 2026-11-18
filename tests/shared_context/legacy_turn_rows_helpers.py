"""Old turns as a whole stored Table and Blob, for tests that only read an old conversation, result or startup inventory.

Each fixtures/legacy_stores/<name>.json is every Behavior row (with its ETag), the batch sequence and every Blob
version that main 5ec4fa1a held after `synthetic_standing_v1` and the retiring `saved_turn`/`claimed_turn` ran,
with counter tokens. configuration.json is the configuration they ran with (evidence path and tokenizer cache
directory replaced) and the digest every stored row is bound to. `stored_turn` writes one into an empty double
and attaches the API the way a restart does; no claim, save, report, bootstrap or install code runs.
"""
import base64
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from shared_context.behavior_assistant_wire import Assignment
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_work_policy import CostObservation, attach_standing_policy
from shared_context.errors import Problem
from shared_context.m3_models import BlobReceipt
from shared_context.m3_store import InMemoryArtifactStore
from test_behavior_server import Harness
from test_behavior_store import NativeEntity, WireTransport, json_response

STORES = Path(__file__).parent / "fixtures" / "legacy_stores"
TABLE = "Behavior"


class StartupTableTransport(WireTransport):
    def send(self, request, **kwargs):
        if request.method == "GET" and urlsplit(request.url).path.rstrip("()") == "/Behavior":
            self.requests.append(request)
            query = parse_qs(urlsplit(request.url).query)
            if query["$filter"][0].startswith(DISCOVERY_FILTER):
                return self._discovery_page(request, query)
            assert query["$filter"] == ["PartitionKey eq 'authority'"]
            items = [{**entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                      "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
                     for (table, partition, _), entity in sorted(self.backing.rows.items())
                     if table == "Behavior" and partition == "authority"]
            return json_response(request, 200, {"value": items}, {})
        return super().send(request, **kwargs)

    def _discovery_page(self, request, query):
        # shortcut: the `Timestamp ge` bound is not applied (no server stamps here); window tests use the
        # test_native_hitl_attention fixture.
        start = query.get("NextRowKey", [None])[0]
        keys = sorted(key for table, partition, key in self.backing.rows if table == "Behavior"
                      and partition == "authority" and "behavior:v1:row:" <= key < "behavior:v1:row;"
                      and (start is None or key >= start))
        top = int(query["$top"][0])
        items = []
        for key in keys[:top]:
            entity = self.backing.rows[("Behavior", "authority", key)]
            items.append({**entity, "Payload": base64.b64encode(entity["Payload"]).decode(),
                          "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]})
        more = {"x-ms-continuation-NextPartitionKey": "authority",
                "x-ms-continuation-NextRowKey": keys[top]} if len(keys) > top else {}
        return json_response(request, 200, {"value": items}, more)


DISCOVERY_FILTER = "PartitionKey eq 'authority' and RowKey ge 'behavior:v1:row:' and RowKey lt 'behavior:v1:row;'"


def read_startup(c):
    return MigrationStore(c.h.composition.clients[0], c.h.config.table.scope).read_inventory()


def outcome_of(call):
    try:
        call()
        return "ok"
    except Problem as error:
        return error.body["error"]["code"]


def open_stored(name, workspace, key_client, monkeypatch):
    """A restarted API on exactly the stored rows of `name`; `turn` is None for the bare standing policy."""
    def forbidden(*args, **kwargs):
        pytest.fail("reading a stored turn must not perform external I/O or launch a process")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.delenv("FA_INSTRUCTION_DEFAULT_PROMPT_PROFILE", raising=False)
    saved = json.loads((STORES / f"{name}.json").read_text(encoding="utf-8"))
    stored = json.loads((STORES / "configuration.json").read_text(encoding="utf-8"))
    # Pinned so stored rows keep verifying; this suite will not notice a change to how sha256 is computed.
    # The stored rows are bound to this digest; today's model_dump would also cover this test's paths.
    monkeypatch.setattr(ServerConfiguration, "sha256", property(lambda _: stored["sha256"]))
    monkeypatch.setattr("test_behavior_server.WireTransport", StartupTableTransport)
    h = Harness(workspace, key_client, configuration_data=stored["configuration"])
    backing = h.table.backing
    # Written directly, not through Backing.apply: apply would mint new ETags and sequence numbers.
    for row in saved["rows"]:
        entity = {**row["entity"], "Payload": row["entity"]["Payload"].encode("utf-8")}
        backing.rows[(TABLE, entity["PartitionKey"], entity["RowKey"])] = NativeEntity(entity, row["etag"])
    backing.sequence = saved["sequence"]
    artifacts = InMemoryArtifactStore(h.config.identity.tenant_id)
    for blob in saved["blobs"]:
        artifacts._versions[(blob["team"], blob["artifact_id"], blob["version_id"])] = (
            BlobReceipt.model_validate_json(json.dumps(blob["receipt"])), blob["data"].encode("utf-8"))
    composition = h.composition
    composition.assistant.artifacts.store = artifacts
    observation = CostObservation.model_validate_json(json.dumps(saved["cost_observation"]))
    composition.work_cost_reader = lambda: observation
    authority = composition.assistant.start()
    control = attach_standing_policy(composition)
    identity = h.config.identity
    human = BehaviorPrincipal(identity.tenant_id, identity.human_oid, identity.team_id, "human", None,
                              frozenset({"read", "approve", "submit", "acknowledge"}))
    turn = None if saved["binding_id"] is None else SimpleNamespace(
        binding=control.records[saved["binding_id"]].binding, candidate_id=saved["candidate_id"],
        assignment=None if saved["assignment"] is None else Assignment.model_validate_json(json.dumps(saved["assignment"])))
    # Every Foundry request the configured executor would send lands in h.calls.
    return SimpleNamespace(h=h, control=control, authority=authority, human=human,
                           sessions=SimpleNamespace(calls=h.calls), turn=turn)


@pytest.fixture
def stored_turn(tmp_path, jwk_client, monkeypatch):
    """Open stored turns by name; the requesting module must import jwk_client."""
    opened = []

    def open_turn(name):
        workspace = tmp_path / f"stored-{len(opened)}"
        workspace.mkdir()
        opened.append(open_stored(name, workspace, jwk_client, monkeypatch))
        return opened[-1]
    try:
        yield open_turn
    finally:
        for case in opened:
            case.h.composition.close()
