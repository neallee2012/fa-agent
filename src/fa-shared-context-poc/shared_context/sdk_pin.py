"""The one Copilot SDK and runtime release FA runs; models, checks and the image path read it from here.

Changing it changes the Hosted state profile (ADR-0009 §3 (c)). Files that cannot import this module are held to it
by tests: the Dockerfile and deploy JSON by ``test_sdk_pin.py``, requirements-hosted.txt by ``test_adapters.py``.

The release is a platform fact, not part of what an owner approves (Neal 2026-11-02): saved records keep the release
they were written with, and this build reads every release in ``KNOWN_RELEASES`` (CTX-03 §4).
"""
from typing import Literal, get_args

SdkVersion = Literal["1.0.26"]
RuntimeVersion = Literal["1.0.103"]
SDK_VERSION: SdkVersion = get_args(SdkVersion)[0]
RUNTIME_VERSION: RuntimeVersion = get_args(RuntimeVersion)[0]
RUNTIME_PATH = f"/opt/copilot/{RUNTIME_VERSION}/prebuilds/linux-x64/copilot-runtime"

# (sdk, runtime) pairs written by earlier builds; add one only with a test that this runtime resumes its saved state.
OLDER_RELEASES: tuple[tuple[str, str], ...] = (("1.0.13", "1.0.83"),)  # test_sdk_checkpoint_cross_version_native
KNOWN_RELEASES = ((SDK_VERSION, RUNTIME_VERSION), *OLDER_RELEASES)
# Saved records validate against these, so a record written by a known earlier release still parses.
KnownSdkVersion = Literal[tuple(sdk for sdk, _ in KNOWN_RELEASES)]
KnownRuntimeVersion = Literal[tuple(runtime for _, runtime in KNOWN_RELEASES)]
# ADR-0009 §3 (c): each state profile names its (sdk, runtime) pair. Material the API sends carries the pair of the
# profile it sends (``native_responses_wire.SENT_RELEASE``), not the release this process runs. Plain strings, since
# this module imports nothing of FA's; ``test_sdk_pin`` holds the keys to ``native_responses_wire``'s constants.
PROFILE_RELEASES = {"foundry-native-v2": OLDER_RELEASES[0], "foundry-native-v3": (SDK_VERSION, RUNTIME_VERSION)}
# ADR-0009 §3 (c) Guard A/B: the Hosted definition ENV naming the pair its image runs ("<sdk>/<runtime>"). Sync writes
# it where it writes the image; the image refuses to start under another pair, so the API may trust it.
RELEASE_ENV = "FA_SDK_RELEASE"
PINNED_RELEASE_TEXT = f"{SDK_VERSION}/{RUNTIME_VERSION}"
# Every v3 version published before the declaration existed (stage 40-43) runs this pair; fixed, not the pin.
UNDECLARED_RELEASE = ("1.0.26", "1.0.103")
