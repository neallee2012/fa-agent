"""Work that BehaviorRuntime.submit once admitted, as the whole store of an offline harness.

Each fixtures/legacy_stores/<name>.json is every Behavior row (with its ETag) and the batch sequence that main
047d2a34 held after the harness was built and the retiring submit ran, so a test that only reads, settles or
refuses over that work starts from the stored rows. Loading replaces whatever the harness constructor wrote; no
submit, register or preflight code runs.
"""
import json
from pathlib import Path

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_server_config import ServerConfiguration
from shared_context.behavior_work_policy import work_policy_key
from test_behavior_server import Harness
from test_behavior_store import NativeEntity

STORES = Path(__file__).parent / "fixtures" / "legacy_stores"
TABLE = "Behavior"


def load_store(backing, name):
    """Make `backing` hold exactly the rows of `name`."""
    load_saved(backing, json.loads((STORES / f"{name}.json").read_text(encoding="utf-8")))


def load_saved(backing, saved):
    backing.rows.clear()
    # Written directly, not through Backing.apply: apply would mint new ETags and sequence numbers.
    for row in saved["rows"]:
        entity = {**row["entity"], "Payload": row["entity"]["Payload"].encode("utf-8")}
        backing.rows[(TABLE, entity["PartitionKey"], entity["RowKey"])] = NativeEntity(entity, row["etag"])
    backing.sequence = saved["sequence"]


def seed_submitted(h):
    """The state `h.submit_direct()` left on a fresh offline `RuntimeHarness(native_wire=False)`."""
    load_store(h.transport.backing, "runtime-submitted")


def native_submitted(workspace, key_client, monkeypatch):
    """A native `Harness` activated as `Harness.activate()` does, holding the rows that `BehaviorRuntime.submit` of
    the first binding left (request "existing-job"); the caller closes `h.composition`."""
    saved = json.loads((STORES / "native-submitted.json").read_text(encoding="utf-8"))
    # Pinned so stored rows keep verifying; the stored configuration is the one they ran with (paths replaced).
    monkeypatch.setattr(ServerConfiguration, "sha256", property(lambda _: saved["sha256"]))
    h = Harness(workspace, key_client, configuration_data=saved["configuration"])
    h.activate()
    load_saved(h.table.backing, saved)
    return h


# The three stores below are the standing synthetic transfer fixture (configuration.json) after work was admitted
# through the retiring WorkPolicyControl.submit/register; `stored_turn` opens them as a restart would.
@pytest.fixture
def admitted(stored_turn):
    """One instruction registered ("registered-once"); the cached record is the registration."""
    c = stored_turn("registered-once")
    (c.record,) = c.control.records.values()
    c.command = InstructionSubmit(request_id="registered-once", content="Synthetic registration gate.")
    c.registration = work_policy_key("registration", c.record.admission_id)
    return c


@pytest.fixture
def admitted_twice(stored_turn):
    """`admitted`, then a second instruction registered ("second-registered"), kept as `c.other`."""
    c = stored_turn("registered-twice")
    c.record, = [r for r in c.control.records.values() if r.request_id == "registered-once"]
    c.other, = [r for r in c.control.records.values() if r.request_id == "second-registered"]
    c.registration = work_policy_key("registration", c.record.admission_id)
    return c


@pytest.fixture
def fixed_work(stored_turn):
    """One fixed work admitted ("fixed-work"); `c.bound` is its binding."""
    c = stored_turn("fixed-work")
    (record,) = c.control.records.values()
    c.bound = record.binding
    return c
