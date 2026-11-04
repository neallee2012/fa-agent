"""ADR-0009 §3: the state profile names four things at once; changing any of them without a new profile fails here.

(a) the wire DTO JSON schemas, (b) the Hosted state store item shapes, (c) the pinned SDK/runtime versions (CTX-03) and
(d) the native checkpoint codecs this image reads. The snapshot is keyed by the profile this image declares and labels
``_Meta`` with (``RUNTIME_PROFILE``): a profile bump (ADR-0009 phase 2d) regenerates it by running only
``test_profile_snapshot_matches_the_current_profile`` with ``FA_REGENERATE_PROFILE_SNAPSHOT=1``, then reviews the diff.

``NATIVE_STATE_PROFILE`` (the send profile) is v2 from 2d-ii and v3 from 3d-ii, while every DTO default
(``default=UNLABELED_STATE_PROFILE, exclude_if=unlabeled_profile``) stays the unlabeled v1 profile, so stored v1 and
unlabeled items still read as v1 and keep their bytes; 2d-i moved ``_Meta``'s label, and this snapshot's key, to v2,
and 3d-i (SDK 1.0.26 / runtime 1.0.103) moves both to v3 while the send profile stays v2 until 3d-ii. From then the
v2 baseline below compares only the wire, (a) and (b); this snapshot pins (c) and (d) (ADR-0009 §3).

JSON schema does not see ``exclude_if`` (which fields a dump leaves out); the profile-label tests below pin that part.

``fixtures/native_v1_wire_h0.json`` is the H0 baseline (stage release 30, ``fb8941f2``, pinned by its SHA). ADR-0009
M1: the "v1 wire 凍結" comparison against it retired with the v1 alias (2026-11-02, stage converged on v2); the fixture
stays only as the source of the stored v1 items whose canonical bytes must not move.

Coordinator decision 2026-10-17 (ADR-0009 §3, M7), two rules:

- Only models Hosted reads or writes are pinned. ``API_ONLY_MODULES`` are left out of the profile snapshot and of both
  v2 baseline's comparison (H0's fixture still lists their models, unchanged); ``test_no_pinned_module_imports_an_api_only
  _one`` keeps that list honest.
- From stage release 31 the v2 wire may only grow: ``V2_BASELINE`` (``fixtures/native_v2_wire_<commit>.json``, named
  after the commit whose code built it) is that baseline. ``v2_drift`` is the H0 comparator made stricter, because a
  release 31 image rejects what it does not know: it also fails on an enum / Literal value, a request variant or a
  model added since the baseline. What passes: a new optional field a dump leaves out by default, and anything listed
  in ``V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE`` and 3d-i's v3 values where no v2 image receives them
  (``_v3_value_old_images_never_receive``). A change that passes needs no new profile: regenerate the
  profile snapshot (above) and review the diff. ``FA_REGENERATE_PROFILE_SNAPSHOT`` does not touch the baseline; it is
  written only by ``python -c "import test_native_profile_snapshot as t; c='<commit>'; t.SNAPSHOT.with_name(
  f'native_v2_wire_{c}.json').write_text(t.v2_baseline_text(c), encoding='utf-8', newline='\\n')"`` (same
  ``PYTHONPATH`` and pydantic) in a checkout whose ``shared_context`` package is ``<commit>``'s; then point
  ``V2_BASELINE`` at the new file, remove the old one and set ``V2_BASELINE_SHA256`` (``test_v2_baseline_is_pinned``
  prints it). Until release 31 every PR that changes the v2 wire regenerates it; the release 31 PR regenerates it once
  more from the main commit it ships (ADR-0009 §3 "v2 快照規則").
"""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib
import inspect
import json
import os
import pkgutil
import re
import typing
from pathlib import Path

import pydantic
import pytest
from pydantic import BaseModel, Field, TypeAdapter, create_model

import shared_context
from shared_context import behavior_native_turn_wire, native_responses_wire, native_turns
from shared_context.behavior_native_turn_wire import (
    NATIVE_REQUEST, NATIVE_STATE_PROFILE, NativeRequest, canonical_bytes)
from shared_context.behavior_sdk_state import AUTO_PROFILE, NATIVE_CODECS, CheckpointProfile, NativeCheckpointRef
from shared_context.native_responses_wire import (
    NATIVE_V2_PROFILE, NATIVE_V3_PROFILE, UNLABELED_STATE_PROFILE, unlabeled_profile)
from shared_context.native_turns import _decode, _json, _Meta
from shared_context.sdk_pin import PROFILE_RELEASES
from test_native_turn_wire import capture, submit

SNAPSHOT = Path(__file__).parent / "fixtures" / "native_profile_snapshot.json"
HOSTED_REQUIREMENTS = Path(shared_context.__file__).resolve().parents[1] / "requirements-hosted.txt"
# (a) the wire DTO modules; (b) every Hosted ``native_*`` module (their models are what the state store holds).
WIRE_MODULES = ("behavior_wire", "behavior_native_turn_wire", "behavior_native_agent_wire",
                "behavior_native_capability_wire", "behavior_sdk_state")
# ``native_*`` modules only the API runs (Context Table / API routes): all but native_cut need azure-data-tables or
# fastapi, which requirements-hosted.txt does not install; native_cut is the API's turn-end trigger.
API_ONLY_MODULES = frozenset({"native_binding", "native_cut", "native_deploy_gate", "native_session_patrol",
                              "native_relay_quota", "native_groups", "native_hitl", "native_hitl_material",
                              "native_routines", "native_teams_relay", "native_version_veto"})
PINNED_PACKAGES = re.compile(r"^(github-copilot-sdk|azure-ai-agentserver-[a-z]+)==\S+$")
# What this image's runtime declares (``FA_NATIVE_TURNS``) and labels ``_Meta`` with, v3 from ADR-0009 3d-i; ``_Meta``'s
# default stays v1 (unlabeled items read as v1), so the label is written explicitly by the runtime.
RUNTIME_PROFILE = NATIVE_V3_PROFILE


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


def _modules(hosted_only=True):
    names = list(WIRE_MODULES)
    names += sorted(info.name for info in pkgutil.iter_modules(shared_context.__path__) if info.name.startswith("native_")
                    and not (hosted_only and info.name in API_ONLY_MODULES))
    return [importlib.import_module("shared_context." + name) for name in names]


@functools.cache  # per class object: a widened test copy is a new class; callers never mutate the result
def _schema(model, mode):
    return _shape(model.model_json_schema(mode=mode))


def _model_shape(model):
    try:
        return _digest({mode: _schema(model, mode) for mode in ("validation", "serialization")})
    except Exception as error:  # a model without a JSON schema is still pinned by name and failure type
        return "no-json-schema:" + type(error).__name__


def _collected(hosted_only=True):
    return {f"{module.__name__.removeprefix('shared_context.')}.{name}": value
            for module in _modules(hosted_only) for name, value in vars(module).items()
            if inspect.isclass(value) and issubclass(value, BaseModel) and value.__module__ == module.__name__
            and not name.startswith("__")}


def _pins_and_codecs():
    pins = sorted(line.strip() for line in HOSTED_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
                  if PINNED_PACKAGES.match(line.strip()))
    return {
        "sdk": {"requirements_hosted": pins,
                "checkpoint_sdk_version": NativeCheckpointRef.model_fields["sdk_version"].default,
                "checkpoint_runtime_version": NativeCheckpointRef.model_fields["runtime_version"].default},
        "checkpoint": {"codecs": dict(sorted(NATIVE_CODECS.items())), "auto_profile": AUTO_PROFILE,
                       "profiles": sorted(CheckpointProfile.model_fields["name"].annotation.__args__)},
    }


def current_snapshot():
    models = {key: _model_shape(model) for key, model in _collected().items()}
    return {
        "state_profile": RUNTIME_PROFILE,
        "native_request": _digest(_shape(NATIVE_REQUEST.json_schema())),
        "models": dict(sorted(models.items())),
        **_pins_and_codecs(),
    }


def test_profile_snapshot_matches_the_current_profile():
    current = current_snapshot()
    if os.environ.get("FA_REGENERATE_PROFILE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        pytest.fail("snapshot regenerated: review the diff and run again without FA_REGENERATE_PROFILE_SNAPSHOT")
    saved = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert saved["state_profile"] == RUNTIME_PROFILE, (
        "the snapshot belongs to another profile; regenerate it for the new profile (ADR-0009 §3)")
    changed = sorted(name for name in saved["models"].keys() | current["models"].keys()
                     if saved["models"].get(name) != current["models"].get(name))
    changed += [part for part in ("native_request", "sdk", "checkpoint") if saved[part] != current[part]]
    assert not changed, (f"wire/state shapes changed under {RUNTIME_PROFILE}: {changed}; regenerate this snapshot if "
                         "test_v2_wire_is_additive_over_its_baseline passes, else bump the profile")


def test_snapshot_covers_the_request_union_the_state_store_items_and_the_checkpoint_reference():
    models = current_snapshot()["models"]
    for name in ("behavior_native_turn_wire.NativeSubmit", "behavior_native_turn_wire.NativeReceipt",
                 "behavior_native_turn_wire.NativeSessionState", "native_turns._Meta", "native_turns._AgentSession",
                 "behavior_sdk_state.NativeCheckpointRef", "native_files_wire.NativeFilesView"):
        assert name in models and not models[name].startswith("no-json-schema"), name


def test_an_added_hosted_field_fails_the_snapshot_until_regenerated(monkeypatch):
    # Never regenerate the fixture from the widened model below, even when the whole file runs with the flag set.
    monkeypatch.delenv("FA_REGENERATE_PROFILE_SNAPSHOT", raising=False)
    widened = create_model("NativeSessionState", __base__=behavior_native_turn_wire.NativeSessionState,
                           __module__=behavior_native_turn_wire.__name__, extra_field=(str | None, None))
    monkeypatch.setattr(behavior_native_turn_wire, "NativeSessionState", widened)
    with pytest.raises(AssertionError, match="NativeSessionState"):
        test_profile_snapshot_matches_the_current_profile()


def test_no_pinned_module_imports_an_api_only_one():
    for module in sorted({model.__module__ for model in _collected().values()}):  # a module with no model pins nothing
        nodes = [node for node in ast.walk(ast.parse(inspect.getsource(importlib.import_module(module))))
                 if isinstance(node, (ast.Import, ast.ImportFrom))]
        names = {name.rpartition(".")[2] for node in nodes
                 for name in [getattr(node, "module", None) or "", *(alias.name for alias in node.names)]}
        assert not names & API_ONLY_MODULES, module


def test_an_api_only_model_changes_without_a_new_profile(monkeypatch):
    from shared_context import native_binding

    monkeypatch.delenv("FA_REGENERATE_PROFILE_SNAPSHOT", raising=False)
    monkeypatch.setattr(native_binding, "NativeBindingRow", _widened(
        native_binding.NativeBindingRow, extra_field=(str | None, None)))  # written by default: refused on Hosted
    test_profile_snapshot_matches_the_current_profile()
    assert v2_drift() == []


# ADR-0009 §3: every state store item written from now on carries its profile; an unlabeled one is v1-era.

def test_conversation_meta_is_written_with_its_profile():
    for profile in (UNLABELED_STATE_PROFILE, RUNTIME_PROFILE):  # no exclude_if: even the v1 label is written
        meta = _Meta(agent_id="agent", hosted_session_id="session", conversation_id="cid", sdk_session_id="sdk",
                     capture=capture(), state_profile=profile)
        assert _json(meta)["state_profile"] == profile


def test_an_unlabeled_conversation_meta_reads_as_v1():
    meta = _Meta(agent_id="agent", hosted_session_id="session", conversation_id="cid", sdk_session_id="sdk",
                 capture=capture())
    unlabeled = {key: value for key, value in _json(meta).items() if key != "state_profile"}
    assert _decode(unlabeled, _Meta).state_profile == UNLABELED_STATE_PROFILE == "foundry-native-v1"


def test_a_v1_submit_digest_does_not_follow_the_profile_constant(monkeypatch):
    """The receipt's ``submit_digest`` is over the unlabeled v1 form; a later profile constant leaves v1 material
    byte-identical (stored receipts still validate, same-RID resends still match) and labels only its own.

    The DTO defaults were built from the constant at import, so patching it here cannot reach them: the models are
    checked directly. Every ``exclude_if=unlabeled_profile`` default is the unlabeled profile, also after 2d-ii flips
    the constant, and the H0 v1 items keep their bytes (module docstring)."""
    from shared_context import behavior_agent_template  # TemplateSettings carries such a default too

    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    labelled = {f"{model.__module__}.{model.__qualname__}.{name}": field.default
                for model in subclasses(BaseModel) if model.__module__.startswith("shared_context.")
                for name, field in model.model_fields.items() if field.exclude_if is unlabeled_profile}
    assert "shared_context.behavior_agent_template.TemplateSettings.state_profile" in labelled
    assert {key: value for key, value in labelled.items() if not unlabeled_profile(value)} == {}
    for name, text in sorted(_h0()["v1_items"].items()):
        assert canonical_bytes(V1_ITEMS[name](text)).decode("utf-8") == text, name

    original = submit("digest-rid")
    before = canonical_bytes(original)
    assert b"state_profile" not in before
    for module in (native_responses_wire, behavior_native_turn_wire):
        monkeypatch.setattr(module, "NATIVE_STATE_PROFILE", "foundry-native-v2")
    assert canonical_bytes(submit("digest-rid")) == before
    assert unlabeled_profile("foundry-native-v1") and not unlabeled_profile("foundry-native-v2")
    assert not unlabeled_profile("foundry-responses-v1")


# The v2 wire is compared with the comparator first written for H0 (ADR-0009 M1 "v1 wire 凍結", retired with the v1
# alias): a field widened into a union or gaining a variant the baseline cannot read, a new default for a field a dump
# leaves out, and anything else that differs is refused. The H0 fixture now only supplies the stored v1 items.

H0_FIXTURE = Path(__file__).parent / "fixtures" / "native_v1_wire_h0.json"
RELEASE_30_SNAPSHOT_SHA256 = "bd3ec14e413dd560e4a71f85e5ec9af07e5e767525edcdac1e633e18dbe2797d"  # fb8941f2's snapshot
H0_FIXTURE_SHA256 = "d60a8a8adebc9a290ab6155fd63c35f2a45c4026e8a50d3f80df2aa166dcd126"  # _digest of the whole fixture
V2_BASELINE = Path(__file__).parent / "fixtures" / "native_v2_wire_8a8608dd.json"  # stage release 31 (docstring)
# _digest of the whole fixture; moves only with v2_baseline_text (module docstring)
V2_BASELINE_SHA256 = "9bf0627d7b4af50c6f74f96739dad0d95900ce502bdb208cb5a30bcbf3eb6e09"
# An exception to "nothing may be added under v2" (the other is ``_v3_value_old_images_never_receive``): an exact
# ``... added`` problem of v2_drift for a value release 31 images provably never receive, each with its proof.
V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE = frozenset({
    # ADR-0009 4d's 已退回 notice kind. No Hosted image ever receives or stores it: (a) no NATIVE_REQUEST variant
    # carries a receipt or a notice (this change leaves the snapshot's ``native_request`` part unchanged); (b) Hosted
    # builds NativeEnvironmentNotice only as ``earlier_version_unfinished``, on a read-time view (``native_turns``
    # ``_refused_view``), never into a stored ``_Rid``; (c) the API sets the slot kinds only on its own response copy
    # (``NativeTurnGateway._bound``'s ``model_copy``, the Teams ``RelayReply``), never into a session command.
    "behavior_native_turn_wire.NativeEnvironmentNotice.kind: enum value 'environment_rolled_back' added",
    "behavior_native_turn_wire.NativeReceipt.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_rolled_back' added",
    "behavior_native_turn_wire.NativeTurnStatus.receipt<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_rolled_back' added",
    "native_group_wire.NativeGroupActivity.turns.items<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_rolled_back' added",
    "native_group_wire.NativeGroupStarted.receipt<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_rolled_back' added",
    "native_turns._Rid.receipt<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_rolled_back' added",
    # ADR-0009 4b-4's 沒有更新 notice kind, by the same proof (a)-(c): it is another slot kind the API sets only on its
    # own response copy (``_bound``, the Teams ``RelayReply``); the snapshot's ``native_request`` part is unchanged.
    "behavior_native_turn_wire.NativeEnvironmentNotice.kind: enum value 'environment_upgrade_vetoed' added",
    "behavior_native_turn_wire.NativeReceipt.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_upgrade_vetoed' added",
    "behavior_native_turn_wire.NativeTurnStatus.receipt<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_upgrade_vetoed' added",
    "native_group_wire.NativeGroupActivity.turns.items<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_upgrade_vetoed' added",
    "native_group_wire.NativeGroupStarted.receipt<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_upgrade_vetoed' added",
    "native_turns._Rid.receipt<NativeReceipt>.environment_notice<NativeEnvironmentNotice>.kind: enum value 'environment_upgrade_vetoed' added",
})
V1_ITEMS = {  # how a stored v1 item is decoded again; its canonical bytes must not move
    "submit": lambda text: NATIVE_REQUEST.validate_json(text),
    "agent_receive": lambda text: NATIVE_REQUEST.validate_json(text),
    "receipt": lambda text: behavior_native_turn_wire.NativeReceipt.model_validate_json(text),
}


def _kind(field):
    """How a dump writes the field: always (required), by default, or only once set (``exclude_if`` of its default)."""
    if field.is_required():
        return "required"
    default = field.get_default(call_default_factory=True, validated_data={})
    return "omitted" if field.exclude or (field.exclude_if is not None and field.exclude_if(default)) else "emitted"


def _fields(model):
    return {name: _kind(field) for name, field in model.model_fields.items()}


def _classes(*types):
    """The models a schema's object nodes come from, by ``$defs`` name (pydantic names a def after its class)."""
    found, todo = {}, list(types)
    while todo:
        value = todo.pop()
        if inspect.isclass(value) and issubclass(value, BaseModel):
            # shortcut: the first class wins a name clash; pydantic then qualifies the def name, so its fields read as
            # unknown and any new field there is refused, never let through
            if value.__name__ not in found:
                found[value.__name__] = value
                todo.extend(field.annotation for field in value.model_fields.values())
        else:
            todo.extend(typing.get_args(value))
    return found


def _request_classes():
    return _classes(NATIVE_REQUEST._type)  # the union the adapter was built from


def _referenced(schema, defs):
    """The ``$defs`` one schema needs, transitively: what pydantic emits next to it."""
    found, todo = {}, [schema]
    while todo:
        node = todo.pop()
        if isinstance(node, dict):
            name = node.get("$ref", "").removeprefix("#/$defs/")
            if name and name not in found:
                found[name] = defs[name]
                todo.append(defs[name])
            todo.extend(node.values())
        elif isinstance(node, list):
            todo.extend(node)
    return found


def _with_defs(schema, defs):
    needed = _referenced(schema, defs)
    return {**schema, "$defs": needed} if needed else schema


def v2_baseline_text(commit):
    """The v2 baseline from the code imported now, which is ``commit``'s (module docstring). Never from pytest. No v1
    items: nothing sends v2 yet and H0 pins the v1 bytes."""
    return _fixture_text("v2_baseline_text", {"commit": commit}, NATIVE_V2_PROFILE, _collected())


def _fixture_text(how, produced_from, state_profile, collected):
    defs, def_fields = {}, {}

    def hoist(schema, classes):
        schema = _shape(schema)
        for name, value in schema.pop("$defs", {}).items():
            assert defs.setdefault(name, value) == value, f"two $defs named {name}"
            if name in classes:
                assert def_fields.setdefault(name, _fields(classes[name])) == _fields(classes[name]), name
        return schema

    models = {}
    for key, model in collected.items():
        schema = model.model_json_schema(mode="validation")
        assert schema == model.model_json_schema(mode="serialization"), key
        models[key] = {"schema": hoist(schema, _classes(model)), "fields": _fields(model)}
    fixture = {
        "produced_from": {**produced_from, "pydantic": pydantic.VERSION,
                          "how": f"{how}() in tests/shared_context/test_native_profile_snapshot.py"},
        "state_profile": state_profile, "models": models,
        "native_request": hoist(NATIVE_REQUEST.json_schema(), _request_classes()),
        "defs": defs, "def_fields": def_fields, **_pins_and_codecs(),
    }
    lines = []  # one def / model per line: small and greppable
    for key, value in sorted(fixture.items()):
        if key in {"defs", "def_fields", "models"}:
            body = ",\n".join(f"  {json.dumps(name)}: {json.dumps(item, sort_keys=True)}"
                              for name, item in sorted(value.items()))
            lines.append(f" {json.dumps(key)}: {{\n{body}\n }}")
        else:
            lines.append(f" {json.dumps(key)}: {json.dumps(value, sort_keys=True)}")
    return "{\n" + ",\n".join(lines) + "\n}\n"


def _h0():
    return json.loads(H0_FIXTURE.read_text(encoding="utf-8"))


def _v2_baseline():
    return json.loads(V2_BASELINE.read_text(encoding="utf-8"))


def wire_drift(h0, schema, classes, root_fields, root, prefix=""):
    """How ``schema`` (current, with its own ``$defs``) is not additive over the baseline schema ``root``. No enum /
    Literal value or union variant may be added: a release 31 image rejects what it does not know (literal_error /
    union_tag_invalid).
    ``prefix``: what the caller puts before each problem, so a listed value is recognised inside a union too."""
    new_defs = schema.get("$defs", {})

    def enum(node):  # a one-value Literal is ``const``; widening it turns it into ``enum``
        return {**{k: v for k, v in node.items() if k != "const"}, "enum": [node["const"]]} if "const" in node else node

    def walk(old, new, where, owner, path):
        if "$ref" in old or "$ref" in new:
            o, n = old.get("$ref", "").rpartition("/")[2], new.get("$ref", "").rpartition("/")[2]
            if (o, n) in path:
                return []
            return walk(h0["defs"][o] if o else old, new_defs[n] if n else new, f"{where}<{n or o}>", n or owner,
                        path | {(o, n)})
        old, new = enum(old), enum(new)
        if "discriminator" in old:
            mapping, now = old["discriminator"]["mapping"], new.get("discriminator", {}).get("mapping", {})
            return [problem for tag, ref in sorted(mapping.items())
                    for problem in (walk({"$ref": ref}, {"$ref": now[tag]}, f"{where}[{tag}]", owner, path)
                                    if tag in now else [f"{where}[{tag}]: union variant removed"])] + [
                f"{where}[{tag}]: union variant added" for tag in sorted(now.keys() - mapping)]
        old_variants, new_variants = old.get("anyOf") or old.get("oneOf"), new.get("anyOf") or new.get("oneOf")
        if old_variants or new_variants:  # each H0 variant must still be accepted by some variant now
            if not old_variants:  # e.g. ``str`` -> ``str | None``: H0 reads one shape only
                return [f"{where}: widened into a union"]
            problems = [problem for variant in old_variants
                        for problem in min((walk(variant, candidate, where, owner, path)
                                            for candidate in new_variants or [new]), key=len)]
            # A candidate whose only differences are listed added values is the old variant, not a new one.
            return problems + [f"{where}: new union variant {json.dumps(candidate, sort_keys=True)} H0 cannot read"
                               for candidate in new_variants or [new]
                               if all(_unlisted([prefix + problem for problem in walk(
                                   variant, candidate, where, owner, path)]) for variant in old_variants)]
        kinds = root_fields if owner is None else h0["def_fields"].get(owner, {})
        live = classes.get(owner)
        now = {name: _kind(field) for name, field in live.model_fields.items()} if live else {}
        problems = []
        for key in sorted(old.keys() | new.keys()):
            a, b = old.get(key), new.get(key)
            if key in {"default", "$defs"}:  # defaults: per property below; $defs: walked through each $ref
                continue
            if key == "enum" and a is not None and b is not None:
                problems += [f"{where}: enum value {value!r} removed" for value in a if value not in b]
                problems += [f"{where}: enum value {value!r} added" for value in b if value not in a]
            elif key == "properties":
                for name in a or {}:
                    if name not in (b or {}):
                        problems.append(f"{where}.{name}: field removed")
                        continue
                    problems += walk(a[name], b[name], f"{where}.{name}", owner, path)
                    if name in kinds and now.get(name) != kinds[name]:
                        problems.append(f"{where}.{name}: written {kinds[name]} in H0, {now.get(name)} now")
                    # left out of the dump, so H0 reads its own default in place of the value now omitted
                    elif kinds.get(name) == "omitted" and a[name].get("default") != b[name].get("default"):
                        problems.append(f"{where}.{name}: omitted default {a[name].get('default')!r} -> "
                                        f"{b[name].get('default')!r}")
                for name in sorted((b or {}).keys() - (a or {}).keys()):
                    if now.get(name) != "omitted":
                        problems.append(f"{where}.{name}: new field is {now.get(name, 'of an unknown model')}, "
                                        "not omitted by default")
            elif key == "required":
                problems += [f"{where}.{name}: new required field" for name in sorted(set(b or ()) - set(a or ()))]
                problems += [f"{where}.{name}: no longer required" for name in sorted(set(a or ()) - set(b or ()))]
            elif key in {"items", "additionalProperties"} and isinstance(a, dict) and isinstance(b, dict):
                problems += walk(a, b, f"{where}.{key}", owner, path)
            elif key == "prefixItems" and len(a or ()) == len(b or ()):
                problems += [p for i, (x, y) in enumerate(zip(a, b)) for p in walk(x, y, f"{where}[{i}]", owner, path)]
            elif a != b:
                problems.append(f"{where}: {key} {a!r} -> {b!r}")
        return problems

    return walk(root, schema, "", None, frozenset())


# ADR-0009 3d-i's v3 values: 'foundry-native-v3' and SDK 1.0.26 / runtime 1.0.103.
V3_ADDED = frozenset(f"enum value {value!r} added" for value in (NATIVE_V3_PROFILE, *PROFILE_RELEASES[NATIVE_V3_PROFILE]))
SENT_TEMPLATE_SETTINGS = re.compile(r"\.settings<TemplateSettings>\.(sdk_version|runtime_version|state_profile)$")
SENT_BODY_PROFILE = re.compile(r"NATIVE_REQUEST\[\w+\]<\w+>\.state_profile")


def _v3_value_old_images_never_receive(problem):
    """A v2 image never receives a v3 value outside NATIVE_REQUEST: it is what a 3d-i image writes about its own run
    (``_Meta``, receipts, checkpoint references, SDK state), kept in that Agent's state store or sent Hosted -> API; a v2
    SID reads it only if the Agent is routed back after its move, which is not supported (ADR-0009 3d-i).

    Under NATIVE_REQUEST only a template's settings may carry one: ``TemplateSettings`` defaults to ``SENT_RELEASE`` and
    ``select_profile`` admits only ``SENT_RELEASE`` and ``SENT_STATE_PROFILES``. A request's own ``state_profile`` may
    gain 'foundry-native-v3' too (a 3d-i image must accept 3d-ii's v3 bodies, ADR-0009 3d-i). The 3d-i API sent v2 or
    a stored original's label (``behavior_native_responses_api.turn_profile``, ``native_agent_relay``,
    ``native_routines``); the 3d-ii API sends v3, but only once its deploy gate finds no v2 SID serving, so no v2 image
    receives it. Any other request position fails."""
    where, _, change = problem.partition(": ")
    if change not in V3_ADDED:
        return False
    return (not where.startswith("NATIVE_REQUEST") or SENT_TEMPLATE_SETTINGS.search(where) is not None
            or SENT_BODY_PROFILE.fullmatch(where) is not None and change == f"enum value {NATIVE_V3_PROFILE!r} added")


def _unlisted(problems):
    return [problem for problem in problems
            if not (problem.endswith(" added") and (problem in V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE
                                                     or _v3_value_old_images_never_receive(problem)))]


def v2_drift():
    """Every way the code imported now is not additive over the v2 baseline (2026-10-17 rule): H0's comparator, but no
    enum / Literal value, request variant or model may be added either; a new optional field left out by default may."""
    return _unlisted(_drift(_v2_baseline()))


def _drift(baseline):
    problems, current = [], _collected()
    # e.g. a model the baseline was built without: none of its fields would ever be compared
    problems += [f"{key}: model not in the baseline" for key in sorted(current.keys() - baseline["models"].keys())]
    for key, entry in sorted(baseline["models"].items()):
        if key.partition(".")[0] in API_ONLY_MODULES:  # H0's fixture lists them; no Hosted image reads them
            continue
        model = current.get(key)
        if model is None:
            problems.append(f"{key}: model removed")
            continue
        classes, seen = {**_classes(model), None: model}, []
        for mode in ("validation", "serialization"):
            schema = _schema(model, mode)
            if schema not in seen:
                seen.append(schema)
                problems += [key + problem for problem in
                             wire_drift(baseline, schema, classes, entry["fields"], entry["schema"], key)]
    problems += ["NATIVE_REQUEST" + problem for problem in wire_drift(
        baseline, _shape(NATIVE_REQUEST.json_schema()), _request_classes(), {}, baseline["native_request"],
        "NATIVE_REQUEST")]
    # ADR-0009 §3 "v2 快照基準在 3d-i 的規則": (c) SDK/runtime and (d) codecs are why a newer profile exists, so once this
    # image declares one the baseline compares wire only; the profile snapshot and the resume test pin them.
    if baseline["state_profile"] == RUNTIME_PROFILE:
        snapshot = _pins_and_codecs()
        problems += [f"{part} changed: {baseline[part]} -> {snapshot[part]}" for part in ("sdk", "checkpoint")
                     if snapshot[part] != baseline[part]]
    return problems


def test_h0_fixture_is_the_release_30_profile_snapshot():
    h0 = _h0()
    derived = {
        "state_profile": h0["state_profile"],
        "native_request": _digest(_with_defs(h0["native_request"], h0["defs"])),
        "models": {key: _digest(dict.fromkeys(("validation", "serialization"), _with_defs(entry["schema"], h0["defs"])))
                   for key, entry in sorted(h0["models"].items())},
        "sdk": h0["sdk"], "checkpoint": h0["checkpoint"],
    }
    text = json.dumps(derived, indent=1, sort_keys=True) + "\n"
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == RELEASE_30_SNAPSHOT_SHA256
    # the parts release 30's snapshot does not cover (fields, def_fields, v1_items) are pinned with the rest
    assert _digest(h0) == H0_FIXTURE_SHA256


def _widened(model, name=None, **fields):
    return create_model(name or model.__name__, __base__=model, __module__=model.__module__, **fields)


def _request(*variants):
    return TypeAdapter(typing.Annotated[typing.Union[variants], Field(discriminator="operation")])


def test_v1_and_unlabeled_items_still_read():
    """ADR-0009 2d-ii: every stored v1 or unlabeled item still decodes as v1 with its canonical bytes unchanged, so
    stored digests still validate and a same-RID resend of v1 material still matches."""
    for name, text in sorted(_h0()["v1_items"].items()):
        item = V1_ITEMS[name](text)
        assert item.state_profile == UNLABELED_STATE_PROFILE != NATIVE_STATE_PROFILE, name
        assert canonical_bytes(item).decode("utf-8") == text, name


def test_a_v2_field_widened_into_a_union_fails(monkeypatch):
    state = behavior_native_turn_wire.NativeSessionState
    etag = state.model_fields["etag"]  # same constraints, now also null
    monkeypatch.setattr(behavior_native_turn_wire, "NativeSessionState", _widened(
        state, etag=(typing.Annotated[etag.annotation, *etag.metadata] | None, ...)))
    assert v2_drift() == ["behavior_native_turn_wire.NativeSessionState.etag: widened into a union"]


def test_a_new_variant_in_a_v2_field_union_fails(monkeypatch):
    monkeypatch.setattr(native_turns, "_Meta", _widened(_Meta, rid=(_Meta.model_fields["rid"].annotation | int, None)))
    assert v2_drift() == ['native_turns._Meta.rid: new union variant {"type": "integer"} H0 cannot read']


def test_a_new_default_for_an_omitted_v2_field_fails(monkeypatch):
    rid = _Meta.model_fields["inflight_rid"].annotation  # still left out at its (new) default
    monkeypatch.setattr(native_turns, "_Meta", _widened(
        _Meta, inflight_rid=(rid, Field(default="h0", exclude_if=lambda value: value in (None, "h0")))))
    assert v2_drift() == ["native_turns._Meta.inflight_rid: omitted default None -> 'h0'"]


def test_a_new_required_v2_field_fails(monkeypatch):
    monkeypatch.setattr(behavior_native_turn_wire, "NativeSessionState", _widened(
        behavior_native_turn_wire.NativeSessionState, extra_field=(str, ...)))
    assert "behavior_native_turn_wire.NativeSessionState.extra_field: new required field" in v2_drift()


def test_a_field_now_written_by_default_fails_and_moves_the_v1_bytes(monkeypatch):
    original = behavior_native_turn_wire.NativeSubmit
    submit = _widened(original, new_conversation_id=(str | None, None))
    monkeypatch.setattr(behavior_native_turn_wire, "NativeSubmit", submit)
    monkeypatch.setitem(globals(), "NATIVE_REQUEST", _request(
        *(submit if variant is original else variant for variant in typing.get_args(NativeRequest.__origin__))))
    assert "behavior_native_turn_wire.NativeSubmit.new_conversation_id: written omitted in H0, emitted now" in v2_drift()
    text = _h0()["v1_items"]["submit"]  # the stored v1 submit would no longer keep its bytes (or its digest)
    assert canonical_bytes(NATIVE_REQUEST.validate_json(text)).decode("utf-8") != text


def test_the_h0_and_v2_baselines_ignore_the_regenerate_flag(monkeypatch, tmp_path):
    before = H0_FIXTURE.read_bytes(), V2_BASELINE.read_bytes()
    monkeypatch.setenv("FA_REGENERATE_PROFILE_SNAPSHOT", "1")
    monkeypatch.setitem(globals(), "SNAPSHOT", tmp_path / "snapshot.json")
    with pytest.raises(pytest.fail.Exception, match="snapshot regenerated"):
        test_profile_snapshot_matches_the_current_profile()
    test_a_changed_v2_field_fails(monkeypatch)  # still refused with the flag set
    assert (H0_FIXTURE.read_bytes(), V2_BASELINE.read_bytes()) == before


def test_a_pin_or_codec_change_fails(monkeypatch, tmp_path):
    """Under the baseline's own profile ``v2_drift`` refuses it; once the image declares a newer profile (ADR-0009
    3d-i) the baseline compares wire only and the profile snapshot refuses it instead."""
    monkeypatch.delenv("FA_REGENERATE_PROFILE_SNAPSHOT", raising=False)
    bumped = tmp_path / "requirements-hosted.txt"
    bumped.write_text(re.sub(r"(?m)^(github-copilot-sdk==)\S+", r"\g<1>0.0.0",
                             HOSTED_REQUIREMENTS.read_text(encoding="utf-8")), encoding="utf-8")

    def refused(part):
        with pytest.raises(AssertionError, match=f"'{part}'"):
            test_profile_snapshot_matches_the_current_profile()
        assert v2_drift() == []
        monkeypatch.setitem(globals(), "RUNTIME_PROFILE", NATIVE_V2_PROFILE)
        # The bumped pin itself differs from the v2 baseline, so "sdk changed" may come with it.
        assert f"{part} changed" in [problem.partition(":")[0] for problem in v2_drift()]

    monkeypatch.setitem(globals(), "HOSTED_REQUIREMENTS", bumped)
    refused("sdk")
    monkeypatch.undo()
    monkeypatch.delenv("FA_REGENERATE_PROFILE_SNAPSHOT", raising=False)
    monkeypatch.setitem(NATIVE_CODECS, "fa-h0-extra-codec", next(iter(NATIVE_CODECS.values())))
    refused("checkpoint")


# 2026-10-17 (ADR-0009 §3, M7): the v2 wire is additive-only over its baseline, by H0's comparator made stricter.

def test_v2_baseline_is_pinned():
    baseline = _v2_baseline()
    assert _digest(baseline) == V2_BASELINE_SHA256
    assert V2_BASELINE.name == f"native_v2_wire_{baseline['produced_from']['commit']}.json"
    assert baseline["state_profile"] == NATIVE_V2_PROFILE and "v1_items" not in baseline
    assert not {key.partition(".")[0] for key in baseline["models"]} & API_ONLY_MODULES
    assert pydantic.VERSION == baseline["produced_from"]["pydantic"], "the schemas are pydantic's output"


def test_v2_wire_is_additive_over_its_baseline(monkeypatch):
    assert v2_drift() == []
    # the allowed growth: a new optional Hosted field left out by default
    monkeypatch.setattr(behavior_native_turn_wire, "NativeSessionState", _widened(
        behavior_native_turn_wire.NativeSessionState,
        reserved=(str | None, Field(default=None, exclude_if=lambda value: value is None))))
    assert v2_drift() == []


def test_a_changed_v2_field_fails(monkeypatch):
    monkeypatch.setattr(behavior_native_turn_wire, "NativeSessionState", _widened(
        behavior_native_turn_wire.NativeSessionState, etag=(int, ...)))
    assert any(problem.startswith("behavior_native_turn_wire.NativeSessionState.etag: type 'string' -> 'integer'")
               for problem in v2_drift())
    monkeypatch.setattr(native_turns, "_Meta", _widened(_Meta, extra_field=(str | None, None)))
    assert "native_turns._Meta.extra_field: new field is emitted, not omitted by default" in v2_drift()


def test_an_added_v2_literal_value_fails_unless_listed(monkeypatch):
    # a release 31 image rejects the unknown value (literal_error); H0's comparator lets a value set grow
    monkeypatch.setattr(behavior_native_turn_wire, "NativeOutcomeReference", _widened(
        behavior_native_turn_wire.NativeOutcomeReference,
        status=(typing.Literal["unknown", "busy_refused", "superseded"], ...)))
    added = "behavior_native_turn_wire.NativeOutcomeReference.status: enum value 'superseded' added"
    assert v2_drift() == [added]
    monkeypatch.setitem(globals(), "V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE",
                        V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE | {added})
    assert v2_drift() == []


def test_a_listed_value_inside_an_optional_model_is_not_a_new_union_variant(monkeypatch):
    # ``NativeReceipt.environment_notice: NativeEnvironmentNotice | None`` gained 4d's listed kind: unlisted, the changed
    # model also reads as a new variant of that union; listed, the old variant still accepts it and nothing is left
    listed = V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE
    assert v2_drift() == []
    monkeypatch.setitem(globals(), "V2_ADDED_VALUES_OLD_IMAGES_NEVER_RECEIVE", frozenset())
    unlisted = v2_drift()
    assert listed <= set(unlisted)
    assert any(problem.startswith("behavior_native_turn_wire.NativeReceipt.environment_notice: new union variant")
               for problem in set(unlisted) - listed)


def test_a_new_v2_request_variant_or_model_fails(monkeypatch):
    # an operation a release 31 image does not know is refused (union_tag_invalid): reserve it before release 31
    probe = _widened(behavior_native_turn_wire.NativeSessionRead, "NativeV2AdditiveProbe",
                     operation=(typing.Literal["v2_additive_probe"], ...))
    monkeypatch.setitem(globals(), "NATIVE_REQUEST", _request(*typing.get_args(NativeRequest.__origin__), probe))
    assert v2_drift() == ["NATIVE_REQUEST[v2_additive_probe]: union variant added"]
    monkeypatch.undo()
    monkeypatch.setattr(behavior_native_turn_wire, "NativeV2AdditiveProbe", probe, raising=False)
    assert v2_drift() == ["behavior_native_turn_wire.NativeV2AdditiveProbe: model not in the baseline"]


def test_a_v3_value_passes_only_where_no_v2_image_receives_it(monkeypatch):
    """3d-i's v3 values pass outside NATIVE_REQUEST, in a request's template settings, and as a request's own v3
    ``state_profile``; anywhere else a v2 image would receive one and refuse it (``_v3_value_old_images_never_receive``)."""
    request = "NATIVE_REQUEST[submit]<NativeSubmit>"
    settings = f"{request}.capture<NativeTurnCapture>.template<AgentTemplate>.settings<TemplateSettings>"
    passing = ["native_turns._Meta.state_profile: enum value 'foundry-native-v3' added",
               "behavior_sdk_state.SdkState.sdk_version: enum value '1.0.26' added",
               f"{settings}.sdk_version: enum value '1.0.26' added",
               f"{settings}.runtime_version: enum value '1.0.103' added",
               f"{settings}.state_profile: enum value 'foundry-native-v3' added",
               f"{request}.state_profile: enum value 'foundry-native-v3' added"]
    failing = [f"{request}.state_profile: enum value '1.0.26' added",
               f"{settings}.checkpoint_profile<CheckpointProfile>.name: enum value 'foundry-native-v3' added",
               f"{request}.capture<NativeTurnCapture>.sdk_version: enum value '1.0.103' added",
               f"{request}.source<NativeAgentOrigin>.state_profile: enum value 'foundry-native-v3' added",
               "behavior_sdk_state.SdkState.sdk_version: enum value '1.0.27' added"]
    assert _unlisted(passing + failing) == failing
    # A real widening: a request's own state_profile gaining an SDK version string is caught.
    submit_model = behavior_native_turn_wire.NativeSubmit
    widened = _widened(submit_model, state_profile=(
        typing.Literal[(*typing.get_args(submit_model.model_fields["state_profile"].annotation), "1.0.26")],
        Field(default=UNLABELED_STATE_PROFILE, exclude_if=unlabeled_profile)))
    variants = tuple(widened if variant is submit_model else variant
                     for variant in typing.get_args(NativeRequest.__origin__))
    monkeypatch.setitem(globals(), "NATIVE_REQUEST", _request(*variants))
    assert v2_drift() == [f"{request}.state_profile: enum value '1.0.26' added"]
