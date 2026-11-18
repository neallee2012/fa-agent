"""Saved instruction turns as the whole stored Table, Blob and console journal of an offline harness.

Each fixtures/legacy_stores/context-*.json is every Behavior row (with its ETag), the batch sequence, every Blob
version and every console journal row that main f36f4a91 held after the retiring submit and controller step ran on
the named fixture; `facts` keeps the values that run returned. Loading replaces whatever the fixture constructor
wrote; no submit, register, issue, save or step code runs.
"""
import base64
import json
from contextlib import closing
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant import key
from shared_context.behavior_instruction import TurnArchive
from shared_context.behavior_sdk_state import CompactionExecution
from shared_context.m3_models import BlobReceipt
from legacy_submit_rows_helpers import load_saved
from sdk_compaction_product_helpers import configured
from web_test_console_frozen_interface_helpers import open_ui

STORES = Path(__file__).parent / "fixtures" / "legacy_stores"
ARTIFACT_TIME = "2026-11-17T00:00:00+00:00"


@pytest.fixture
def pinned_artifact_clock(monkeypatch):
    """Request before the case fixture: Team artifacts approved while it is built (the SOP a profile copies) carry
    the time the stored rows were bound to, not the wall clock."""
    monkeypatch.setattr("shared_context.m3_artifacts.now", lambda: ARTIFACT_TIME)


def load_case(h, name, journal=None):
    """Make `h` hold exactly the stored rows and Blobs of `name` (and `journal` its console rows); returns facts."""
    saved = json.loads((STORES / f"{name}.json").read_text(encoding="utf-8"))
    load_saved(h.transport.backing, saved)
    store = h.assistant.artifacts.store
    store._versions.clear()
    for blob in saved["blobs"]:
        store._versions[(blob["team"], blob["artifact_id"], blob["version_id"])] = (
            BlobReceipt.model_validate(blob["receipt"]), base64.b64decode(blob["data_base64"]))
    if journal is not None:
        with closing(sqlite3.connect(journal)) as db, db:
            for table, rows in saved["journal"].items():
                db.execute(f"DELETE FROM {table}")
                for row in rows:
                    db.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(row))})", row)
    return saved["facts"]


def stored_saved_turn(c, name="context-saved-none"):
    """`saved_turn(c)` on a `real_interface`/`profile_interface` case, read back from its stored state."""
    c.browser.ui.close()
    facts = load_case(c.h, name, c.journal)
    # The console reads its journal on open, so it is reopened over the stored rows.
    c.browser = open_ui(c)
    return facts["draft"], facts["task"], facts.get("path")


def stored_turn(control, request_id):
    """The registered turn of `request_id` and the TurnArchive its save wrote."""
    control.refresh()
    record = next(r for r in control.records.values() if r.request_id == request_id)
    akey = key("text-archive", record.binding.job_id)
    saved = TurnArchive.model_validate_json(json.dumps(control.store.read_snapshot((akey,))[akey].payload["data"]))
    return SimpleNamespace(binding=record.binding, saved=saved, reference=saved.archive,
                           assignment=SimpleNamespace(task=SimpleNamespace(instruction=record.profile.instruction)))


def stored_checkpoint(assistant, name):
    """`checkpoint_case` after its compaction turn (and `facts["followups"]` later turns) settled, as stored."""
    h, control, human = configured(assistant)
    facts = load_case(h, name)
    observed = stored_turn(control, facts["observed_request_id"])
    return SimpleNamespace(
        h=h, control=control, human=human, binding=observed.binding, assignment=observed.assignment,
        reference=observed.reference, turns=[stored_turn(control, request) for request in facts["followups"]],
        fs=SimpleNamespace(compaction=CompactionExecution.model_validate(facts["compaction"])),
        previous=SimpleNamespace(sha256=facts["previous_sha256"]))
