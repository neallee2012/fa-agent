"""ADR-0009 3d-i: the pinned runtime resumes a checkpoint the older known release saved; without that pair, refused.

``fixtures/ctx03-runtime-1.0.83`` is ``sdk_checkpoint_local.py <root> create <runtime 1.0.83>`` run on main (620ea4ff)
with SDK 1.0.13, then VACUUMed; neither the transport credential canary nor the private system canary is in it.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from shared_context.behavior_sdk_state import StoredSdkArchive
from shared_context.sdk_pin import OLDER_RELEASES, RUNTIME_VERSION, SDK_VERSION

FIXTURE = Path(__file__).parent / "fixtures" / "ctx03-runtime-1.0.83"
SCRIPT = Path(__file__).with_name("sdk_checkpoint_local.py")
# The pin with OLDER_RELEASES emptied, set before any model module imports it (the Literal types are built at import).
WITHOUT_OLDER = """
import asyncio, sys
from pathlib import Path
from typing import Literal
from shared_context import sdk_pin
sdk_pin.OLDER_RELEASES = ()
sdk_pin.KNOWN_RELEASES = ((sdk_pin.SDK_VERSION, sdk_pin.RUNTIME_VERSION),)
sdk_pin.KnownSdkVersion = Literal[sdk_pin.SDK_VERSION]
sdk_pin.KnownRuntimeVersion = Literal[sdk_pin.RUNTIME_VERSION]
sdk_pin.PROFILE_RELEASES = dict.fromkeys(sdk_pin.PROFILE_RELEASES, sdk_pin.KNOWN_RELEASES[0])  # sent material too
import sdk_checkpoint_local
asyncio.run(sdk_checkpoint_local.execute(Path(sys.argv[1]), "resume", Path(sys.argv[2])))
"""


def _fixture(tmp_path):
    root = tmp_path / "saved-by-1.0.83"
    shutil.copytree(FIXTURE, root)
    return root


def test_the_pinned_runtime_resumes_what_the_older_known_release_saved(tmp_path):
    runtime = Path(os.environ["M2_TEST_RUNTIME"])
    assert runtime.is_file(), "Provision the pinned runtime; do not silently skip this acceptance"
    root = _fixture(tmp_path)
    saved = StoredSdkArchive.model_validate_json((root / "create-reference.json").read_bytes())
    assert (saved.sdk_version, saved.runtime_version) in OLDER_RELEASES
    result = subprocess.run([sys.executable, str(SCRIPT), str(root), "resume", str(runtime)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    resumed = json.loads((root / "resume-proof.json").read_bytes())
    assert resumed["tools"] == [] and "Restored original source CATALOG_" in resumed["answer"]
    assert resumed["sdk_stop"]["graceful"] and resumed["sdk_stop"]["runtime_version"] == RUNTIME_VERSION
    assert resumed["private_system_filtered"] and resumed["transport_credential_absent"]
    # The new checkpoint names the release that wrote it; the old one is kept as it was.
    written = StoredSdkArchive.model_validate_json((root / "resume-reference.json").read_bytes())
    assert (written.sdk_version, written.runtime_version) == (SDK_VERSION, RUNTIME_VERSION)
    assert (root / "create-reference.json").read_bytes() == (FIXTURE / "create-reference.json").read_bytes()


def test_the_same_checkpoint_is_refused_at_load_once_its_release_is_not_known(tmp_path):
    root = _fixture(tmp_path)
    paths = [str(Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"), str(SCRIPT.parent)]
    # Refused while parsing the reference, before any runtime starts: the runtime path is never used.
    result = subprocess.run([sys.executable, "-c", WITHOUT_OLDER, str(root), str(tmp_path / "no-runtime")],
                            capture_output=True, text=True, encoding="utf-8", timeout=120,
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(paths)})
    assert result.returncode != 0
    assert "StoredSdkArchive" in result.stderr and "literal_error" in result.stderr, result.stderr
    assert not (root / "work-resume").exists() and not (root / "resume-reference.json").exists()
