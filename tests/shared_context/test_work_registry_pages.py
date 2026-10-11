"""Scoped work registry capacity through original producers; no SDK, model or network."""

import json
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_store import _json
from shared_context.behavior_work_registry import (
    PAGE_SIZE, WRITER_ENV, WorkRegistryHead, WorkRegistryPage, head_key, page_key, read_registry,
)
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_work_policy_submit_compatibility import post, route


@pytest.fixture
def runtime_path():
    return Path(__file__)


def test_seven_hundred_thirtieth_admission_keeps_the_original_registry_and_succeeds(assistant, monkeypatch):
    monkeypatch.setenv("FA_DELIVERY_PENDING_INDEX_WRITER", "true")
    monkeypatch.setenv("FA_WORK_REGISTRY_WRITER", "false")
    h, control, _, starts = route(assistant)
    backing = h.transport.backing
    apply = backing.apply

    def apply_touched(table, operations):
        # Keep the existing fake EGT/CAS implementation, but avoid deep-copying
        # thousands of untouched immutable entities on every synthetic transaction.
        previous = backing.rows
        keys = {(table, operation[1]["PartitionKey"], operation[1]["RowKey"]) for operation in operations}
        backing.rows = {key: previous[key] for key in keys if key in previous}
        try:
            result = apply(table, operations)
        except BaseException:
            backing.rows = previous
            raise
        backing.rows = {**previous, **backing.rows}
        return result

    monkeypatch.setattr(backing, "apply", apply_touched)
    entity = h.store._entity
    refused = []

    def serialized(key, value):
        try:
            return entity(key, value)
        except Problem as error:
            if error.status == 413:
                refused.append((getattr(value, "row_id", None), len(_json(value)), error.body["error"]["code"]))
            raise

    monkeypatch.setattr(h.store, "_entity", serialized)
    for number in range(1, 731):
        if number == 730:
            original = h.store.read_snapshot((control.registry_key,))[control.registry_key]
            monkeypatch.setenv("FA_WORK_REGISTRY_WRITER", "true")
        command = InstructionSubmit(request_id=f"registry-capacity-{number:03}", content="Keep this original work.")
        response = post(h, "submit", command)
        if number >= 728 or response.status_code != 200:
            row = h.store.read_snapshot((control.registry_key,))[control.registry_key]
            print({"admission": number, "status": response.status_code,
                   "legacy_entries": len(row.payload["data"]["entries"]), "rejected": refused}, flush=True)
        assert response.status_code == 200, (number, response.text, refused)
    assert len(starts) == 730 and not h.endpoint.requests
    assert h.store.read_snapshot((control.registry_key,))[control.registry_key] == original


def test_new_registry_pages_keep_old_bytes_original_id_and_reader_rollback(assistant, monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "false")
    h, control, human, _ = route(assistant)
    first = InstructionSubmit(request_id="original-legacy", content="Original retained work.")
    assert post(h, "submit", first).status_code == 200
    legacy = h.store.read_snapshot((control.registry_key,))[control.registry_key]
    monkeypatch.setenv(WRITER_ENV, "true")
    second = InstructionSubmit(request_id="new-paged", content="New separately retained work.")
    accepted = post(h, "submit", second)
    assert accepted.status_code == 200, accepted.text
    assert h.store.read_snapshot((control.registry_key,))[control.registry_key] == legacy
    assert len(read_registry(h.store, control.policy).entries) == 2
    original = copy.deepcopy(h.transport.backing.rows)
    assert post(h, "submit", second).content == accepted.content
    assert post(h, "submit-reconcile", second).content == accepted.content
    assert h.transport.backing.rows == original
    assert post(h, "submit", second.model_copy(update={"content": "Changed."})).status_code == 409
    monkeypatch.setenv(WRITER_ENV, "false")
    control.records.clear()
    control.refresh()
    assert accepted.json()["binding_id"] in control.records
    assert post(h, "submit-reconcile", second).content == accepted.content
    denied = post(h, "submit", InstructionSubmit(request_id="reader-only-new", content="Do not rewrite the old row."))
    assert denied.status_code == 503 and h.store.read_snapshot((control.registry_key,))[control.registry_key] == legacy
    control.revoke(human, "withdraw-current-read")
    assert post(h, "submit-reconcile", second).status_code == 403
    assert not h.endpoint.requests


def test_registry_page_missing_or_wrong_scope_is_not_a_legacy_fallback(assistant, monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h, control, _, _ = route(assistant)
    accepted = post(h, "submit", InstructionSubmit(request_id="paged-source", content="Original."))
    assert accepted.status_code == 200
    key = page_key(control.registry_key, 1)
    read = h.store.read_snapshot

    def missing(keys):
        return {name: None if name == key else row for name, row in read(keys).items()}

    monkeypatch.setattr(h.store, "read_snapshot", missing)
    with pytest.raises(Problem) as unavailable:
        control.refresh()
    assert unavailable.value.body["error"]["code"] == "WORK_POLICY_REGISTRY_INVALID"
    monkeypatch.setattr(h.store, "read_snapshot", read)
    h.mutate(key, index_key="another-agent-index")
    with pytest.raises(Problem) as mismatch:
        control.refresh()
    assert mismatch.value.body["error"]["code"] == "WORK_POLICY_REGISTRY_INVALID"


def test_registry_response_loss_only_recovers_the_same_registration(assistant, monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h, control, _, _ = route(assistant)
    from shared_context.behavior_work_policy import derive_work_binding, work_policy_key
    command = InstructionSubmit(request_id="lost-registration", content="Keep exactly this input.")
    admission, _ = derive_work_binding(control.policy, command.request_id, instruction=command)
    registration = work_policy_key("registration", admission)
    submit = h.store._submit
    lost = []

    def disappear(operations, request):
        result = submit(operations, request)
        if request.request_id == registration:
            lost.append(True)
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "Synthetic registration response lost.")
        return result

    monkeypatch.setattr(h.store, "_submit", disappear)
    assert post(h, "submit", command).status_code == 503
    assert lost == [True]
    monkeypatch.setattr(h.store, "_submit", submit)
    observed = post(h, "submit-reconcile", command)
    assert observed.status_code == 200 and observed.json()["state"] == "not_admitted"
    accepted = post(h, "submit", command)
    assert accepted.status_code == 200, accepted.text
    index = read_registry(h.store, control.policy)
    assert index.entries == (admission,)
    assert WorkRegistryHead.model_validate_json(json.dumps(
        h.store.read_snapshot((head_key(control.registry_key),))[head_key(control.registry_key)].payload["data"])).entry_count == 1
    assert not h.endpoint.requests


def test_registry_tail_rollover_and_conflict_keep_exact_original_order(assistant, monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h, control, human, _ = route(assistant)
    records = []
    for number in range(PAGE_SIZE):
        command = InstructionSubmit(request_id=f"tail-{number}", content="Original retained registration.")
        records.append(control.register(human, command.request_id,
            hashlib.sha256(command.model_dump_json().encode()).hexdigest(), instruction=command))
    first_page = h.store.read_snapshot((page_key(control.registry_key, 1),))[page_key(control.registry_key, 1)]
    first, second = (InstructionSubmit(request_id=identifier, content="Concurrent original registration.")
                     for identifier in ("tail-first", "tail-second"))
    from shared_context.behavior_work_policy import derive_work_binding, work_policy_key
    identifier, _ = derive_work_binding(control.policy, second.request_id, instruction=second)
    target = work_policy_key("registration", identifier)
    reached, release = Event(), Event()
    submit = h.store._submit
    paused = []

    def pause(operations, request):
        if request.request_id == target and not paused:
            paused.append(True)
            reached.set()
            assert release.wait(20)
        return submit(operations, request)

    monkeypatch.setattr(h.store, "_submit", pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        other = pool.submit(post, h, "submit", second)
        assert reached.wait(20)
        try:
            accepted = post(h, "submit", first)
            assert accepted.status_code == 200, accepted.text
        finally:
            release.set()
        rejected = other.result(timeout=20)
    assert rejected.status_code == 412, rejected.text
    assert post(h, "submit-reconcile", second).json()["state"] == "not_admitted"
    accepted_second = post(h, "submit", second)
    assert accepted_second.status_code == 200, accepted_second.text
    expected = tuple(item.admission_id for item in records) + (
        derive_work_binding(control.policy, first.request_id, instruction=first)[0], identifier)
    assert read_registry(h.store, control.policy).entries == expected
    assert h.store.read_snapshot((page_key(control.registry_key, 1),))[page_key(control.registry_key, 1)] == first_page
    tail = WorkRegistryPage.model_validate_json(json.dumps(h.store.read_snapshot(
        (page_key(control.registry_key, 2),))[page_key(control.registry_key, 2)].payload["data"]))
    assert tail.entries == expected[-2:] and not h.endpoint.requests
