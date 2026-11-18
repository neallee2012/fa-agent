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
from shared_context.source_materials import _payload_hash
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_work_policy_submit_compatibility import route


@pytest.fixture
def runtime_path():
    return Path(__file__)


def test_registry_page_missing_or_wrong_scope_is_not_a_legacy_fallback(assistant, monkeypatch):
    monkeypatch.setenv(WRITER_ENV, "true")
    h, control, _, _ = route(assistant)
    legacy = h.store.read_snapshot((control.registry_key,))[control.registry_key]
    h.seed([
        (head_key(control.registry_key), WorkRegistryHead(
            index_key=control.registry_key, policy_id=control.policy.policy_id, policy_sha256=control.policy.sha256,
            legacy_sha256=_payload_hash(legacy.payload), entry_count=1, page_count=1)),
        (page_key(control.registry_key, 1), WorkRegistryPage(
            index_key=control.registry_key, number=1, entries=("paged-source-admission",))),
    ])
    # The listed admission has no registration row: refusing it is the page check's doing, never the registration read's.
    monkeypatch.setattr(control, "_registered", lambda _: pytest.fail("registry pages were not checked first"))
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
