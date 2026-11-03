"""Files that cannot import ``sdk_pin`` name its runtime release; only the image names the runtime path."""

import json
import re
from pathlib import Path

from shared_context.sdk_pin import RUNTIME_PATH, RUNTIME_VERSION, SDK_VERSION

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "fa-shared-context-poc"


def test_the_image_installs_and_points_at_the_pinned_runtime():
    dockerfile = (SOURCE / "Dockerfile.behavior-assistant-desktop").read_text(encoding="utf-8")
    assert set(re.findall(r"/opt/copilot/([^/\s]+)", dockerfile)) == {RUNTIME_VERSION}
    assert f"ENV BEHAVIOR_ASSISTANT_RUNTIME_PATH={RUNTIME_PATH}" in dockerfile.splitlines()


def test_no_deploy_profile_overrides_the_images_runtime():
    # A definition ENV outlives the image it was written for: the 1.0.83 path broke the 1.0.103 image (dev 4400).
    setting = [profile.relative_to(ROOT).as_posix() for profile in sorted(ROOT.glob("deploy/*/*.json"))
               if "BEHAVIOR_ASSISTANT_RUNTIME_PATH" in profile.read_text(encoding="utf-8")]
    assert setting == []


def test_saved_records_are_typed_to_read_every_known_release():
    # The narrow pin or a written-out release would reject a record an earlier known release saved (CTX-03 §4).
    narrow = re.compile(r'\b(SdkVersion|RuntimeVersion)\b|\b(sdk|runtime)_version: Literal\["[0-9]')
    offenders = [f"{path.name}:{number}" for path in sorted((SOURCE / "shared_context").glob("*.py"))
                 if path.name != "sdk_pin.py"
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if narrow.search(line)]
    assert offenders == []


def test_each_profile_names_a_known_pair_and_sent_material_carries_the_send_profiles():
    from shared_context.behavior_agent_template import TemplateSettings
    from shared_context.native_responses_wire import (
        NATIVE_STATE_PROFILE, NATIVE_V2_PROFILE, NATIVE_V3_PROFILE, SENT_RELEASE)
    from shared_context.sdk_pin import KNOWN_RELEASES, PROFILE_RELEASES

    assert PROFILE_RELEASES.keys() == {NATIVE_V2_PROFILE, NATIVE_V3_PROFILE}
    assert set(PROFILE_RELEASES.values()) <= set(KNOWN_RELEASES)
    assert SENT_RELEASE == PROFILE_RELEASES[NATIVE_STATE_PROFILE]
    settings = TemplateSettings()
    assert (settings.sdk_version, settings.runtime_version) == SENT_RELEASE


def test_saved_records_name_one_known_release_never_a_mix():
    import importlib
    import pkgutil
    import types

    import pydantic
    import pytest

    import shared_context
    from shared_context.behavior_agent_template import TemplateSettings
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.sdk_pin import KNOWN_RELEASES

    record = assemble_prompt("fa-instruction-agent-role-v4").record
    for model, values in ((TemplateSettings, {}), (type(record), record.model_dump(mode="json"))):
        for sdk, runtime in KNOWN_RELEASES:
            model.model_validate_json(json.dumps({**values, "sdk_version": sdk, "runtime_version": runtime}))
        with pytest.raises(pydantic.ValidationError, match="not one known release"):
            model.model_validate_json(json.dumps({**values, "sdk_version": "1.0.26", "runtime_version": "1.0.83"}))
    unchecked, checked = [], set()
    for info in pkgutil.iter_modules(shared_context.__path__):
        for value in vars(importlib.import_module("shared_context." + info.name)).values():
            fields = getattr(value, "model_fields", None) if isinstance(value, type) else None
            if (fields and {"sdk_version", "runtime_version"} <= fields.keys()
                    and types.NoneType not in getattr(fields["sdk_version"].annotation, "__args__", ())):
                checked.add(value)
                if "_release" not in value.__pydantic_decorators__.model_validators:
                    unchecked.append(value.__qualname__)
    assert unchecked == [] and len(checked) >= 20, (unchecked, len(checked))


def test_a_record_saved_under_the_other_known_pair_still_matches():
    """ADR-0009 §3 (c): stored prompt records and exported template settings are compared without their pair, so a
    3d-ii flip of the send profile (or reading a 3d-ii record on a 3d-i image) does not look like a changed source."""
    from shared_context.behavior_agent_template import TemplateSettings
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.behavior_wire import same_but_release
    from shared_context.sdk_pin import KNOWN_RELEASES

    fresh = assemble_prompt("fa-instruction-agent-role-v4").record
    for current in (fresh, TemplateSettings()):
        values = current.model_dump(mode="json")
        for sdk, runtime in KNOWN_RELEASES:
            saved = type(current).model_validate_json(json.dumps({**values, "sdk_version": sdk, "runtime_version": runtime}))
            assert same_but_release(saved, current)
            assert (saved == current) is ((sdk, runtime) == (current.sdk_version, current.runtime_version))
    assert not same_but_release(fresh, assemble_prompt("fa-instruction-v4").record)
    assert not same_but_release(None, fresh) and same_but_release(None, None)
    assert not same_but_release(TemplateSettings(), TemplateSettings(state_profile="foundry-responses-v1"))
