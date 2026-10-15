"""ADR-0009 §3: the state profile names four things at once; changing any of them without a new profile fails here.

(a) the wire DTO JSON schemas, (b) the Hosted state store item shapes, (c) the pinned SDK/runtime versions (CTX-03) and
(d) the native checkpoint codecs this image reads. The snapshot is keyed by ``NATIVE_STATE_PROFILE``: a profile bump
(ADR-0009 phase 2d) regenerates it by running only ``test_profile_snapshot_matches_the_current_profile`` with
``FA_REGENERATE_PROFILE_SNAPSHOT=1``, then reviews the diff.

JSON schema does not see ``exclude_if`` (which fields a dump leaves out); the profile-label tests below pin that part.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import pkgutil
import re
from pathlib import Path

import pytest
from pydantic import BaseModel

import shared_context
from shared_context import behavior_native_turn_wire, native_responses_wire
from shared_context.behavior_native_turn_wire import NATIVE_REQUEST, NATIVE_STATE_PROFILE, canonical_bytes
from shared_context.behavior_sdk_state import AUTO_PROFILE, NATIVE_CODECS, CheckpointProfile, NativeCheckpointRef
from shared_context.native_responses_wire import UNLABELED_STATE_PROFILE, unlabeled_profile
from shared_context.native_turns import _decode, _json, _Meta
from test_native_turn_wire import capture, submit

SNAPSHOT = Path(__file__).parent / "fixtures" / "native_profile_snapshot.json"
HOSTED_REQUIREMENTS = Path(shared_context.__file__).resolve().parents[1] / "requirements-hosted.txt"
# (a) the wire DTO modules; (b) every Hosted ``native_*`` module (their models are what the state store holds).
WIRE_MODULES = ("behavior_wire", "behavior_native_turn_wire", "behavior_native_agent_wire",
                "behavior_native_capability_wire", "behavior_sdk_state")
PINNED_PACKAGES = re.compile(r"^(github-copilot-sdk|azure-ai-agentserver-[a-z]+)==\S+$")


def _shape(node, *, schema=True):
    """The schema without prose: ``title``/``description`` are dropped only where they annotate a schema, never where
    they name a property or a definition."""
    if isinstance(node, list):
        return [_shape(item) for item in node]
    if not isinstance(node, dict):
        return node
    if not schema:
        return {key: _shape(value) for key, value in node.items()}
    return {key: _shape(value, schema=key not in {"properties", "$defs", "patternProperties"})
            for key, value in node.items() if key not in {"title", "description"}}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _modules():
    names = list(WIRE_MODULES)
    names += sorted(info.name for info in pkgutil.iter_modules(shared_context.__path__) if info.name.startswith("native_"))
    return [importlib.import_module("shared_context." + name) for name in names]


def _model_shape(model):
    try:
        return _digest({mode: _shape(model.model_json_schema(mode=mode)) for mode in ("validation", "serialization")})
    except Exception as error:  # a model without a JSON schema is still pinned by name and failure type
        return "no-json-schema:" + type(error).__name__


def current_snapshot():
    models = {}
    for module in _modules():
        for name, value in vars(module).items():
            if (inspect.isclass(value) and issubclass(value, BaseModel) and value.__module__ == module.__name__
                    and not name.startswith("__")):
                models[f"{module.__name__.removeprefix('shared_context.')}.{name}"] = _model_shape(value)
    pins = sorted(line.strip() for line in HOSTED_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
                  if PINNED_PACKAGES.match(line.strip()))
    return {
        "state_profile": NATIVE_STATE_PROFILE,
        "native_request": _digest(_shape(NATIVE_REQUEST.json_schema())),
        "models": dict(sorted(models.items())),
        "sdk": {"requirements_hosted": pins,
                "checkpoint_sdk_version": NativeCheckpointRef.model_fields["sdk_version"].default,
                "checkpoint_runtime_version": NativeCheckpointRef.model_fields["runtime_version"].default},
        "checkpoint": {"codecs": dict(sorted(NATIVE_CODECS.items())), "auto_profile": AUTO_PROFILE,
                       "profiles": sorted(CheckpointProfile.model_fields["name"].annotation.__args__)},
    }


def test_profile_snapshot_matches_the_current_profile():
    current = current_snapshot()
    if os.environ.get("FA_REGENERATE_PROFILE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        pytest.fail("snapshot regenerated: review the diff and run again without FA_REGENERATE_PROFILE_SNAPSHOT")
    saved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert saved["state_profile"] == NATIVE_STATE_PROFILE, (
        "the snapshot belongs to another profile; regenerate it for the new profile (ADR-0009 §3)")
    changed = sorted(name for name in saved["models"].keys() | current["models"].keys()
                     if saved["models"].get(name) != current["models"].get(name))
    assert not changed, f"wire/state shapes changed under {NATIVE_STATE_PROFILE}; bump the profile: {changed}"
    for part in ("native_request", "sdk", "checkpoint"):
        assert saved[part] == current[part], f"{part} changed under {NATIVE_STATE_PROFILE}; bump the profile"


def test_snapshot_covers_the_request_union_the_state_store_items_and_the_checkpoint_reference():
    models = current_snapshot()["models"]
    for name in ("behavior_native_turn_wire.NativeSubmit", "behavior_native_turn_wire.NativeReceipt",
                 "behavior_native_turn_wire.NativeSessionState", "native_turns._Meta", "native_turns._AgentSession",
                 "behavior_sdk_state.NativeCheckpointRef", "native_files_wire.NativeFilesView"):
        assert name in models and not models[name].startswith("no-json-schema"), name


def test_an_added_field_without_a_new_profile_fails_the_snapshot(monkeypatch):
    from pydantic import create_model

    # Never regenerate the fixture from the widened model below, even when the whole file runs with the flag set.
    monkeypatch.delenv("FA_REGENERATE_PROFILE_SNAPSHOT", raising=False)
    widened = create_model("NativeSessionState", __base__=behavior_native_turn_wire.NativeSessionState,
                           __module__=behavior_native_turn_wire.__name__, extra_field=(str | None, None))
    monkeypatch.setattr(behavior_native_turn_wire, "NativeSessionState", widened)
    with pytest.raises(AssertionError, match="NativeSessionState"):
        test_profile_snapshot_matches_the_current_profile()


# ADR-0009 §3: every state store item written from now on carries its profile; an unlabeled one is v1-era.

def test_conversation_meta_is_written_with_its_profile():
    meta = _Meta(agent_id="agent", hosted_session_id="session", conversation_id="cid", sdk_session_id="sdk",
                 capture=capture())
    assert _json(meta)["state_profile"] == NATIVE_STATE_PROFILE


def test_an_unlabeled_conversation_meta_reads_as_v1():
    meta = _Meta(agent_id="agent", hosted_session_id="session", conversation_id="cid", sdk_session_id="sdk",
                 capture=capture())
    unlabeled = {key: value for key, value in _json(meta).items() if key != "state_profile"}
    assert _decode(unlabeled, _Meta).state_profile == UNLABELED_STATE_PROFILE == "foundry-native-v1"


def test_a_v1_submit_digest_does_not_follow_the_profile_constant(monkeypatch):
    """The receipt's ``submit_digest`` is over the unlabeled v1 form; a later profile constant leaves v1 material
    byte-identical (stored receipts still validate, same-RID resends still match) and labels only its own."""
    original = submit("digest-rid")
    before = canonical_bytes(original)
    assert b"state_profile" not in before
    for module in (native_responses_wire, behavior_native_turn_wire):
        monkeypatch.setattr(module, "NATIVE_STATE_PROFILE", "foundry-native-v2")
    assert canonical_bytes(submit("digest-rid")) == before
    assert unlabeled_profile("foundry-native-v1") and not unlabeled_profile("foundry-native-v2")
    assert not unlabeled_profile("foundry-responses-v1")
