"""deploy/azd/stage_profile.py: the stage.json-shaped profile of an azd environment (ADR-0010 #stage-private-1026),
offline and synthetic. It must add only the CI fields azd never writes and pass the stage CI's own profile checks."""

import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
DELTA = Path(__file__).with_name("fixtures") / "azd_stage_profile_delta.json"
PUBLIC = "A" * 43 + "="
DEPLOY_CLIENT = "7a8b9c0d-8888-4999-8aaa-bbbbccccdddd"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The same synthetic azd environment as the azd profile tests, in the rehearsal's shape: private, Cosmos memory,
# gpt-5.4-mini only.
BASE = load("azd_profile_cases", ROOT / "tests" / "shared_context" / "test_azd_profile.py")
PRIVATE = {"FA_NETWORK_MODE": "private", "FA_COSMOS_ENDPOINT": BASE.COSMOS, "FA_MODELS": "gpt-5.4-mini",
           "FA_MEMORY_CHAT_MODEL": "gpt-5.4-mini", "FA_AGENT_ONBOARDING": "true",
           "FA_AGENT_ONBOARDING_ACCOUNT_ROLE": "true", "FA_ENABLE_WEBIQ": "true"}
CI_FIELDS = {"FA_DEPLOY_CLIENT_ID": DEPLOY_CLIENT, "FA_FEED_APP": "ca-feed-dev", "FA_FEED_PUBLIC_KEY": PUBLIC}


@pytest.fixture(scope="module")
def stage_profile():
    return load("fa_azd_stage_profile", ROOT / "deploy" / "azd" / "stage_profile.py")


def test_adds_only_the_ci_fields_azd_never_writes(stage_profile):
    values = BASE.values(**PRIVATE, **CI_FIELDS)
    profile, azd = stage_profile.build(values), stage_profile.fa_profile.build(values)
    delta = {key: value for key, value in profile.items() if key not in azd}
    assert delta == json.loads(DELTA.read_text(encoding="utf-8"))
    assert {key: profile[key] for key in azd} == azd
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    assert set(stage) <= set(profile)  # every field of the stage profile, so ci_deploy reads it the same way


def test_without_the_ci_values_it_is_the_azd_profile(stage_profile):
    values = BASE.values(**PRIVATE)
    assert stage_profile.build(values) == stage_profile.fa_profile.build(values)


def test_feed_app_and_its_public_key_come_together(stage_profile):
    for half in ({"FA_FEED_APP": "ca-feed-dev"}, {"FA_FEED_PUBLIC_KEY": PUBLIC}):
        with pytest.raises(SystemExit, match="set together"):
            stage_profile.build(BASE.values(**PRIVATE, **half))
    secret = stage_profile.build(BASE.values(**PRIVATE, **CI_FIELDS, FA_FEED_SIGNING_SECRET="other-key"))
    assert secret["api_secret_environment"] == {"FA_FEED_MCP_SIGNING_KEY": "other-key"}


def test_written_profile_passes_the_ci_checks_and_finds_the_copied_runtime(stage_profile, tmp_path, monkeypatch,
                                                                          capsys):
    runtime = tmp_path / "azd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "config.json").write_bytes(b'{"deployed": "bytes"}\n')
    (runtime / "fresh-inputs.json").write_bytes(b"{}\n")
    path = stage_profile.write(tmp_path / "stage", BASE.values(**PRIVATE, **CI_FIELDS), runtime)
    assert path == tmp_path / "stage" / "stage.json"
    monkeypatch.setenv("FA_DEPLOY_CONFIG", str(path))
    ci = load("ci_deploy_azd_stage", ROOT / "deploy" / "stage" / "ci_deploy.py")
    ci.STAGE, ci.CONFIG = ci.load_config(path), ci.config_path(path)
    ci.RUNTIME = ci.CONFIG.parent / "runtime"  # main()'s default without --runtime-dir, as in deploy-staging.yml
    # The api step compares the deployed configuration with these bytes, so they are azd's own, not regenerated.
    assert ci._config_bytes() == b'{"deployed": "bytes"}\n'
    assert ci.feed_config() == ("ca-feed-dev", {"FEED_MCP_PUBLIC_KEY": PUBLIC})
    assert ci.api_secrets() == {"FA_FEED_MCP_SIGNING_KEY": "fa-feed-mcp-signing-key"}
    assert ci.release() == "none"  # no release.json next to it: a rehearsal names no release
    ci.check()
    ci.meta()
    assert f"deploy_client_id={DEPLOY_CLIENT}" in capsys.readouterr().out


def test_a_laptop_profile_without_the_ci_identity_passes_check_but_not_meta(stage_profile, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "config.json").write_bytes(b"{}\n")
    values = BASE.values(**PRIVATE, FA_FEED_APP="ca-feed-dev", FA_FEED_PUBLIC_KEY=PUBLIC)
    path = stage_profile.write(tmp_path / "stage", values, runtime)
    ci = load("ci_deploy_azd_laptop", ROOT / "deploy" / "stage" / "ci_deploy.py")
    ci.STAGE = ci.load_config(path)
    ci.check()
    with pytest.raises(SystemExit, match="no deploy_client_id"):
        ci.meta()


def test_a_rerun_replaces_the_runtime_so_no_earlier_file_survives(stage_profile, tmp_path):
    runtime = tmp_path / "azd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "config.json").write_bytes(b'{"run": 1}\n')
    (runtime / "old-only.json").write_bytes(b"{}\n")
    out = tmp_path / "stage"
    stage_profile.write(out, BASE.values(**PRIVATE), runtime)
    (runtime / "old-only.json").unlink()
    (runtime / "config.json").write_bytes(b'{"run": 2}\n')
    stage_profile.write(out, BASE.values(**PRIVATE), runtime)
    assert sorted(p.name for p in (out / "runtime").iterdir()) == ["config.json"]
    assert (out / "runtime" / "config.json").read_bytes() == b'{"run": 2}\n'
    assert not (out / "runtime.new").exists()


def test_a_missing_runtime_stops_before_anything_is_written(stage_profile, tmp_path):
    with pytest.raises(SystemExit, match="config.json does not exist"):
        stage_profile.write(tmp_path / "stage", BASE.values(**PRIVATE), tmp_path / "none")
    assert not (tmp_path / "stage").exists()
