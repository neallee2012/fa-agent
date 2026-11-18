"""Old (job-bound) workbench conversations as stored rows, for tests that only read them.

API side: each fixtures/legacy_stores/console-*.json is every Behavior row (with its ETag), the batch sequence and
every Blob version that main f36f4a91 held after the retired workbench submit (and, for some, cancel) ran on the
named fixture; `facts` keeps the command, admission and cancellation outcome that run recorded, plus values the
test reads. Workbench side: the journal is written through InstructionStore and Journal the way the retired bridge
submit and cancel wrote it, from those models. No submit, preflight, reconcile or cancel code runs.
"""
import base64
import json
from pathlib import Path

import pytest

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.m3_models import BlobReceipt
from legacy_submit_rows_helpers import load_saved
from web_test_console import instructions as console_instructions
from web_test_console.adapter import SteadyConsoleConfig, canonical, sha
from web_test_console.freeform import CancellationReceipt, InstructionReply
from web_test_console_frozen_interface_helpers import open_ui

STORES = Path(__file__).parent / "fixtures" / "legacy_stores"


def load_rows(h, name):
    """Make `h` hold exactly the stored rows and Blobs of `name`; returns its facts."""
    saved = json.loads((STORES / f"{name}.json").read_text(encoding="utf-8"))
    load_saved(h.transport.backing, saved)
    store = getattr(getattr(h, "assistant", None), "artifacts", None)
    if store is not None:
        store = store.store
        store._versions.clear()
        for blob in saved["blobs"]:
            store._versions[(blob["team"], blob["artifact_id"], blob["version_id"])] = (
                BlobReceipt.model_validate(blob["receipt"]), base64.b64decode(blob["data_base64"]))
    return saved["facts"]


def seed_submitted(console, command, admission):
    """The local thread, command and submit intent the retired bridge submit left once the API admitted `command`."""
    control, journal = console.instructions, console.journal
    with pytest.MonkeyPatch.context() as patch:
        # The thread and draft are minted with the original request id the stored binding derives from.
        patch.setattr(console_instructions.secrets, "token_hex", lambda _: command.request_id.removeprefix("text-"))
        control.store.new_thread()
    body = command.model_dump(mode="json")
    control.store.prepare(body, freeze=True)
    task = control.task(command.request_id)
    created = console.clock().isoformat()
    intent = {"method": "POST", "path": "/work-policy/v1/submit", "request_id": body["request_id"],
              "body_sha256": sha(canonical(body)), "created_at": created, "configuration_sha256": console.config.sha256}
    if isinstance(console.config, SteadyConsoleConfig):
        intent.update(console_id=console.config.console_id, authorization_mode=console.config.authorization_mode)
    else:
        intent["scope_id"] = console.config.scope_id
    intent["instruction_connection"] = control.connection.model_dump(mode="json")
    observed = control.submission_context(task)
    intent["instruction_context"] = {
        "conversation_id": observed.conversation_id, "turn_number": observed.turn_number, "model": observed.model}
    intent["body"] = body
    journal.begin(task.task_id, "submit", intent)
    journal.finish(task.task_id, "submit", {"state": "CONFIRMED", "observed_at": console.clock().isoformat(),
                                            "admission": admission.model_dump(mode="json")})
    return task


def seed_cancel(console, task, generation, outcome):
    """The cancel intent the retired bridge cancel wrote and the outcome it saw (`receipt` or an error `code`)."""
    command = wire.Command(request_id=sha(canonical(["instruction-cancel", console.config.console_id, task.task_id])))
    body = command.model_dump(mode="json")
    created = console.clock().isoformat()
    console.journal.begin(task.task_id, "cancel", {
        "method": "POST", "path": f"/work-policy/v1/bindings/{task.binding_id}/cancel",
        "request_id": command.request_id, "body": body, "body_sha256": sha(canonical(body)),
        "job_id": task.job_id, "generation": generation, "created_at": created,
    })
    if "receipt" in outcome:
        receipt = CancellationReceipt.model_validate_json(canonical(outcome["receipt"]))
        saved = {"state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
                 "observed_at": console.clock().isoformat(), "automatic_retry": False}
    else:
        saved = {"state": "UNKNOWN", "code": outcome["code"], "observed_at": console.clock().isoformat(),
                 "automatic_retry": False}
    console.journal.finish(task.task_id, "cancel", saved)


def seed_journal(console, facts):
    """Every instruction (and cancellation) `facts` recorded, written into `console`'s journal."""
    for item in facts["instructions"]:
        task = seed_submitted(console, InstructionSubmit.model_validate_json(canonical(item["command"])),
                              InstructionReply.model_validate_json(canonical(item["admission"])))
        if item.get("cancel") is not None:
            seed_cancel(console, task, item["cancel"]["generation"], item["cancel"]["outcome"])


def stored_console(c, name):
    """`c` (a workbench case) restarted over the stored API rows of `name` and its seeded journal; returns facts."""
    facts = load_rows(c.h, name)
    c.control.refresh()
    seed_journal(c.browser.app.state.console, facts)
    # The console reads its journal on open, so it is reopened over the seeded rows.
    c.browser.ui.close()
    c.browser = open_ui(c)
    # As after the original setup: only what the test does next is in the backend call log.
    c.calls.clear()
    return facts
